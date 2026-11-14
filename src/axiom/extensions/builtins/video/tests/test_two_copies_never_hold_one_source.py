# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Two copies of the capture service never both hold a source; a switch is by overlap.

VID-007, ADR-182, spec-video §Overlap: the source's claim
(``axiom.infra.pending_claim``, item ``<state>/video/claims/<name>``) decides who
captures. A second copy finds the claim held by a live owner and does not open the
source; it reports ``waiting`` and takes over when the first lets go. A claim whose
owner is dead is taken over at once. A copy that finds it no longer holds the
claim stops capturing.
"""

from __future__ import annotations

import json
import time

from axiom.extensions.builtins.video import state as vs
from axiom.extensions.builtins.video.capture import SessionState, SourceSupervisor
from axiom.extensions.builtins.video.connector import VideoConnector
from axiom.infra import pending_claim

from .fakes import (
    EOF,
    FOREVER,
    FakeDecoder,
    FakeFrameSource,
    dead_claim,
    frame_bytes,
    registered_kind,
)
from .harness import wait_for

CAM = VideoConnector(name="cam", source={"kind": "fake"}, fps_cap=10)


def _copy(state_dir, decoder, **kw):
    return SourceSupervisor(
        CAM,
        state_dir=state_dir,
        decoder=decoder,
        rng=lambda: 0.0,
        poll_s=0.01,
        status_interval_s=0.0,
        **kw,
    )


def test_a_second_copy_waits_and_takes_over_when_the_first_lets_go(state_dir):
    with registered_kind(FakeFrameSource()):
        first_decoder, second_decoder = (
            FakeDecoder(default=[FOREVER]),
            FakeDecoder(default=[FOREVER]),
        )
        first, second = _copy(state_dir, first_decoder), _copy(state_dir, second_decoder)
        first.start()
        try:
            assert wait_for(lambda: first.counters["frames"] > 0, timeout=10)
            second.start()
            assert wait_for(lambda: second.state is SessionState.WAITING, timeout=10)
            assert second_decoder.sessions == [], "the waiting copy never opened the source"
            holder = vs.claim_holder("cam", state_dir)
            assert holder["alive"] and holder["token"] == first.claim_token

            first.stop(timeout=10)
            assert first_decoder.sessions[-1].closed
            assert wait_for(lambda: second.counters["frames"] > 0, timeout=10), (
                "the second copy took over"
            )
            assert second.state is SessionState.RUNNING
        finally:
            first.stop(timeout=10)
            second.stop(timeout=10)


def test_the_waiting_copy_is_visible_to_status(state_dir):
    with registered_kind(FakeFrameSource()):
        first, second = (
            _copy(state_dir, FakeDecoder(default=[FOREVER])),
            _copy(state_dir, FakeDecoder()),
        )
        first.start()
        try:
            assert wait_for(lambda: first.state is SessionState.RUNNING, timeout=10)
            second.start()
            assert wait_for(lambda: second.state is SessionState.WAITING, timeout=10)
            standby = wait_for(lambda: vs.read_standby("cam", state_dir), timeout=5)
            assert [s["copy_id"] for s in standby] == [second.copy_id]
            assert vs.read_live("cam", state_dir)["copy_id"] == first.copy_id, (
                "the waiter does not overwrite"
            )
        finally:
            second.stop(timeout=10)
            first.stop(timeout=10)
        assert vs.read_standby("cam", state_dir) == []


def test_a_claim_left_by_a_dead_copy_is_taken_over(state_dir):
    dead_claim(vs.claim_item("cam", state_dir))
    with registered_kind(FakeFrameSource(replayable=True)):
        sup = _copy(state_dir, FakeDecoder([[frame_bytes(8, 6), EOF]]))
        assert sup.run() is SessionState.FINISHED
    assert sup.counters["frames"] == 1


def test_the_claim_is_released_when_capture_ends(state_dir):
    with registered_kind(FakeFrameSource(replayable=True)):
        _copy(state_dir, FakeDecoder([[EOF]])).run()
    assert vs.claim_holder("cam", state_dir) is None
    assert not pending_claim.claim_path(vs.claim_item("cam", state_dir)).exists()


def test_a_copy_that_loses_its_claim_stops_capturing(state_dir):
    with registered_kind(FakeFrameSource()):
        decoder = FakeDecoder(default=[FOREVER])
        sup = _copy(state_dir, decoder)
        sup.start()
        try:
            assert wait_for(lambda: sup.state is SessionState.RUNNING, timeout=10)
            path = pending_claim.claim_path(vs.claim_item("cam", state_dir))
            taken = json.loads(path.read_text())
            path.write_text(
                json.dumps({**taken, "token": "somebody-else"})
            )  # taken over while wedged
            assert wait_for(lambda: sup.state is SessionState.WAITING, timeout=10)
            assert decoder.sessions[-1].closed
            assert json.loads(path.read_text())["token"] == "somebody-else", (
                "it did not take it back"
            )
        finally:
            sup.stop(timeout=10)


def test_a_waiting_copy_gives_up_when_the_source_is_stopped(state_dir):
    wanted = {"cam": True}
    with registered_kind(FakeFrameSource()):
        first = _copy(state_dir, FakeDecoder(default=[FOREVER]))
        second_decoder = FakeDecoder()
        second = _copy(state_dir, second_decoder, should_run=lambda: wanted["cam"])
        first.start()
        try:
            assert wait_for(lambda: first.state is SessionState.RUNNING, timeout=10)
            thread = second.start()
            assert wait_for(lambda: second.state is SessionState.WAITING, timeout=10)
            wanted["cam"] = False
            thread.join(timeout=10)
            assert not thread.is_alive() and second.state is SessionState.STOPPED
            assert second_decoder.sessions == []
        finally:
            first.stop(timeout=10)


def test_a_running_copy_stops_when_the_source_is_stopped(state_dir):
    wanted = {"cam": True}
    with registered_kind(FakeFrameSource()):
        decoder = FakeDecoder(default=[FOREVER])
        sup = _copy(state_dir, decoder, should_run=lambda: wanted["cam"])
        thread = sup.start()
        assert wait_for(lambda: sup.state is SessionState.RUNNING, timeout=10)
        wanted["cam"] = False
        thread.join(timeout=10)
    assert not thread.is_alive() and sup.state is SessionState.STOPPED
    assert decoder.sessions[-1].closed
    assert vs.claim_holder("cam", state_dir) is None


class _SlowToVerify(FakeFrameSource):
    """A camera that takes longer to answer than the claim's stale window."""

    def __init__(self, delay_s: float, **kw) -> None:
        super().__init__(**kw)
        self.delay_s = delay_s

    def verify(self):
        time.sleep(self.delay_s)
        return super().verify()


def test_a_holder_waiting_out_a_long_back_off_is_not_taken_for_dead(state_dir, monkeypatch):
    """Found by unplugging a relayed camera: after five failures the back-off passed the
    30 s stale window, status read the live holder as stopped, and anything starting the
    source in that window would have taken its claim."""
    monkeypatch.setattr(vs, "CLAIM_STALE_S", 1.0)
    with registered_kind(FakeFrameSource(offline_verifies=99)):
        sup = SourceSupervisor(
            CAM,
            state_dir=state_dir,
            decoder=FakeDecoder(),
            sleep=lambda s: time.sleep(3.0),
            rng=lambda: 0.0,
            poll_s=0.01,
            status_interval_s=0.0,
        )
        sup.start()
        try:
            assert wait_for(lambda: sup.counters["reconnects"] >= 1, timeout=10)
            time.sleep(2.0)  # inside the 3 s back-off, past the 1 s stale window
            holder = vs.claim_holder("cam", state_dir)
            assert holder and holder["alive"], "the holder is alive and must read as alive"
            rival = _copy(state_dir, FakeDecoder())
            rival.start()
            assert wait_for(lambda: rival.state is SessionState.WAITING, timeout=5)
            assert vs.claim_holder("cam", state_dir)["token"] == sup.claim_token
            rival.stop(timeout=10)
        finally:
            sup.stop(timeout=10)


def test_a_holder_waiting_on_a_slow_camera_is_not_taken_for_dead(state_dir, monkeypatch):
    monkeypatch.setattr(vs, "CLAIM_STALE_S", 1.0)
    with registered_kind(_SlowToVerify(3.0)):
        sup = _copy(state_dir, FakeDecoder(default=[FOREVER]))
        sup.start()
        try:
            time.sleep(2.0)  # inside the slow verify
            holder = vs.claim_holder("cam", state_dir)
            assert holder and holder["alive"]
            assert wait_for(lambda: sup.state is SessionState.RUNNING, timeout=10)
        finally:
            sup.stop(timeout=10)


def test_a_wedged_holder_still_goes_stale(state_dir, monkeypatch):
    """Keeping the claim fresh is for waits that are bounded by design, not for a hang."""
    monkeypatch.setattr(vs, "CLAIM_STALE_S", 1.0)
    with registered_kind(FakeFrameSource()):

        class Hangs(FakeDecoder):
            def open(self, *a, **k):
                session = super().open(*a, **k)
                session.read = lambda timeout: time.sleep(30)  # a read that never returns
                return session

        sup = _copy(state_dir, Hangs())
        sup.start()
        try:
            assert wait_for(lambda: vs.claim_holder("cam", state_dir) is not None, timeout=5), (
                "it took the claim"
            )
            assert wait_for(lambda: not vs.claim_holder("cam", state_dir)["alive"], timeout=10)
        finally:
            sup.request_stop()
