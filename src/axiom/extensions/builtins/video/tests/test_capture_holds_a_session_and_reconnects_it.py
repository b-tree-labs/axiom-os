# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The capture supervisor: one session per source, frames onto the bus, reconnect on loss.

spec-video §Design. Width and height come from ``verify``, re-probed on each
(re)connect. Stdout EOF or a stall ends a session; a lost EPHEMERAL session
reconnects with exponential back-off (1 s to 60 s) plus jitter and counts it
(VID-005); for a REPLAYABLE kind EOF is the end and the state is ``finished``.
Counters: frames, drops, reconnects, last-frame age. Capture runs whether or not
anything is subscribed (VID-009). Played against a fake decoder, no binaries.
"""

from __future__ import annotations

import os

import pytest

from axiom.extensions.builtins.video import state as vs
from axiom.extensions.builtins.video.capture import (
    BACKOFF_CAP_S,
    FIRST_FRAME_GRACE_S,
    Backoff,
    SessionState,
    SourceSupervisor,
    stall_timeout_s,
)
from axiom.extensions.builtins.video.connector import VideoConnector

from .fakes import (
    EOF,
    FOREVER,
    STALL,
    FakeDecoder,
    FakeFrameSource,
    RecordingSleep,
    frame_bytes,
    registered_kind,
)
from .harness import wait_for

CAM = VideoConnector(name="cam", source={"kind": "fake"}, fps_cap=10)


def _supervisor(state_dir, decoder, *, stop_after=None, **kw):
    ref: list = []
    sleep = RecordingSleep(ref, stop_after=stop_after)
    sup = SourceSupervisor(
        kw.pop("connector", CAM),
        state_dir=state_dir,
        decoder=decoder,
        sleep=sleep,
        rng=lambda: 0.0,
        poll_s=0.01,
        status_interval_s=0.0,
        **kw,
    )
    ref.append(sup)
    return sup, sleep


@pytest.fixture
def replayable(state_dir):
    source = FakeFrameSource(replayable=True)
    with registered_kind(source):
        yield source


@pytest.fixture
def live(state_dir):
    source = FakeFrameSource(replayable=False)
    with registered_kind(source):
        yield source


def _drain(sub):
    out = []
    while (frame := sub.get_nowait()) is not None:
        out.append(frame)
    return out


# ---- one session --------------------------------------------------------------


def test_frames_reach_the_bus_gapless_within_one_session(state_dir, replayable):
    decoder = FakeDecoder([[frame_bytes(8, 6, 1), frame_bytes(8, 6, 2), frame_bytes(8, 6, 3), EOF]])
    sup, _ = _supervisor(state_dir, decoder)
    sub = sup.bus.subscribe("t", maxsize=10)
    assert sup.run() is SessionState.FINISHED
    frames = _drain(sub)
    assert [f.seq for f in frames] == [0, 1, 2]
    assert len({f.session_id for f in frames}) == 1
    assert {f.stream for f in frames} == {"cam"}
    assert frames[0].array.shape == (6, 8, 3)
    assert frames[1].array[0, 0].tolist() == [2, 2, 2]
    assert [f.pts for f in frames] == pytest.approx([0.0, 0.1, 0.2]), (
        "pts on the fps-capped timeline"
    )


def test_the_decoder_is_opened_with_the_verified_size_and_the_fps_cap(state_dir, replayable):
    decoder = FakeDecoder([[EOF]])
    _supervisor(
        state_dir, decoder, connector=VideoConnector(name="cam", source={"kind": "fake"}, fps_cap=4)
    )[0].run()
    assert decoder.log[0] == ("open", 8, 6, 4)


def test_capture_runs_with_nothing_subscribed(state_dir, replayable):
    """VID-009: a started source is captured whether or not anything consumes it."""
    decoder = FakeDecoder([[frame_bytes(8, 6)] * 4 + [EOF]])
    sup, _ = _supervisor(state_dir, decoder)
    sup.run()
    assert sup.counters["frames"] == 4 and sup.counters["drops"] == 0


def test_a_replayable_source_ends_at_eof_without_reconnecting(state_dir, replayable):
    decoder = FakeDecoder([[frame_bytes(8, 6), EOF]])
    sup, sleep = _supervisor(state_dir, decoder)
    assert sup.run() is SessionState.FINISHED
    assert len(decoder.sessions) == 1 and sleep.delays == []
    assert sup.counters["reconnects"] == 0
    assert vs.read_live("cam", state_dir)["state"] == "finished"


def test_half_a_frame_at_the_end_is_counted_as_a_drop(state_dir, replayable):
    decoder = FakeDecoder([[frame_bytes(8, 6), ("partial", 100)]])
    sup, _ = _supervisor(state_dir, decoder)
    sup.run()
    assert sup.counters["frames"] == 1 and sup.counters["drops"] == 1


def test_a_full_subscription_counts_toward_the_source_drops(state_dir, replayable):
    decoder = FakeDecoder([[frame_bytes(8, 6)] * 3 + [EOF]])
    sup, _ = _supervisor(state_dir, decoder)
    sup.bus.subscribe("never_reads", maxsize=1)
    sup.run()
    assert sup.counters["drops"] == 2


def test_every_session_is_closed(state_dir, replayable):
    decoder = FakeDecoder([[frame_bytes(8, 6), EOF]])
    _supervisor(state_dir, decoder)[0].run()
    assert all(s.closed for s in decoder.sessions)


# ---- losing a live session -------------------------------------------------------


def test_a_lost_live_session_reconnects_with_a_new_session_and_seq_from_zero(state_dir, live):
    decoder = FakeDecoder([[frame_bytes(8, 6)] * 2 + [EOF], [frame_bytes(8, 6)] * 3 + [EOF]])
    sup, sleep = _supervisor(state_dir, decoder, stop_after=2)
    sub = sup.bus.subscribe("t", maxsize=10)
    assert sup.run() is SessionState.STOPPED
    frames = _drain(sub)
    assert [f.seq for f in frames] == [0, 1, 0, 1, 2]
    assert frames[0].session_id != frames[2].session_id
    assert sup.counters["reconnects"] == 2
    assert sleep.delays == [1.0, 1.0], "a session that delivered frames resets the back-off"


def test_back_off_grows_while_the_source_stays_away(state_dir):
    source = FakeFrameSource(offline_verifies=5)
    with registered_kind(source):
        sup, sleep = _supervisor(state_dir, FakeDecoder(), stop_after=5)
        sup.run()
    assert sleep.delays == [1.0, 2.0, 4.0, 8.0, 16.0]
    assert sup.counters["reconnects"] == 5
    snap = vs.read_live("cam", state_dir)
    assert "offline" in snap["error"] and snap["fix"] == "plug it back in"


def test_a_stall_ends_the_session_and_it_reconnects(state_dir, live):
    decoder = FakeDecoder([[frame_bytes(8, 6), STALL], [frame_bytes(8, 6), EOF]])
    sup, _ = _supervisor(state_dir, decoder, stop_after=2)
    sup.run()
    assert len(decoder.sessions) == 2 and all(s.closed for s in decoder.sessions)
    assert sup.counters["frames"] == 2 and sup.counters["reconnects"] == 2


def test_the_first_frame_of_a_session_gets_a_grace_the_rest_do_not(state_dir, replayable):
    decoder = FakeDecoder([[frame_bytes(8, 6), frame_bytes(8, 6), EOF]])
    _supervisor(state_dir, decoder)[0].run()
    first, second, *_ = decoder.sessions[0].timeouts
    assert first == pytest.approx(stall_timeout_s(10) + FIRST_FRAME_GRACE_S)
    assert second == pytest.approx(stall_timeout_s(10))


def test_frame_size_is_re_probed_on_every_connect(state_dir):
    source = FakeFrameSource(sizes=((8, 6), (4, 2)))
    decoder = FakeDecoder([[frame_bytes(8, 6), EOF], [frame_bytes(4, 2), EOF]])
    with registered_kind(source):
        sup, _ = _supervisor(state_dir, decoder, stop_after=2)
        sub = sup.bus.subscribe("t", maxsize=10)
        sup.run()
    assert [entry[1:3] for entry in decoder.log if entry[0] == "open"] == [(8, 6), (4, 2)]
    assert [f.array.shape for f in _drain(sub)] == [(6, 8, 3), (2, 4, 3)]
    assert source.verify_calls == 2


# ---- what other processes read -----------------------------------------------------


def test_the_snapshot_is_what_status_reads_from_another_process(state_dir, replayable):
    decoder = FakeDecoder([[frame_bytes(8, 6)] * 3 + [EOF]])
    sup, _ = _supervisor(state_dir, decoder)
    sup.run()
    snap = vs.read_live("cam", state_dir)
    assert snap["name"] == "cam" and snap["pid"] == os.getpid()
    assert snap["frames"] == 3 and snap["drops"] == 0 and snap["reconnects"] == 0
    assert snap["session_id"] and snap["last_frame_at"] is not None
    assert snap["probe"] == {"codec": "h264", "width": 8, "height": 6, "fps": 10.0}


def test_stop_ends_a_running_capture_and_releases_its_claim(state_dir, live):
    decoder = FakeDecoder(default=[FOREVER])
    sup, _ = _supervisor(state_dir, decoder)
    thread = sup.start()
    assert wait_for(lambda: sup.counters["frames"] > 5, timeout=10)
    assert sup.state is SessionState.RUNNING
    sup.stop(timeout=10)
    assert not thread.is_alive()
    assert sup.state is SessionState.STOPPED
    assert decoder.sessions[-1].closed
    assert vs.claim_holder("cam", state_dir) is None
    assert vs.read_live("cam", state_dir)["state"] == "stopped"


def test_fps_is_measured_from_the_frames_that_arrived(state_dir, live):
    decoder = FakeDecoder(default=[FOREVER])
    sup, _ = _supervisor(state_dir, decoder)
    sup.start()
    try:
        assert wait_for(lambda: sup.counters["frames"] > 20, timeout=10)
        assert sup.counters["fps"] and sup.counters["fps"] > 0
    finally:
        sup.stop(timeout=10)


# ---- the numbers -----------------------------------------------------------------


def test_back_off_doubles_from_one_second_to_the_cap_plus_jitter():
    backoff = Backoff(base=1.0, cap=60.0, jitter=0.5, rng=lambda: 1.0)
    assert [backoff.next_delay() for _ in range(8)] == [1.5, 3.0, 6.0, 12.0, 24.0, 48.0, 90.0, 90.0]
    backoff.reset()
    assert backoff.next_delay() == 1.5
    assert BACKOFF_CAP_S == 60.0


def test_jitter_is_never_negative_and_never_more_than_its_fraction():
    for r in (0.0, 0.25, 0.999):
        delay = Backoff(base=1.0, cap=60.0, jitter=0.25, rng=lambda r=r: r).next_delay()
        assert 1.0 <= delay <= 1.25


def test_a_stall_is_three_frame_intervals_but_never_under_five_seconds():
    assert stall_timeout_s(10) == 5.0
    assert stall_timeout_s(1) == 5.0


def test_a_lost_session_says_why_while_it_reconnects(state_dir, live):
    """status must say why a source is reconnecting, not only that it is."""
    decoder = FakeDecoder([[frame_bytes(8, 6)] * 2 + [EOF], [frame_bytes(8, 6), STALL]])
    snapshots: list[dict] = []
    ref: list = []
    sleep = RecordingSleep(ref, stop_after=2)
    sup = SourceSupervisor(
        CAM,
        state_dir=state_dir,
        decoder=decoder,
        sleep=lambda s: (snapshots.append(sup.snapshot()), sleep(s)),
        rng=lambda: 0.0,
        poll_s=0.01,
        status_interval_s=0.0,
    )
    ref.append(sup)
    sup.run()
    ended, stalled = snapshots
    assert ended["state"] == "reconnecting" and "ended" in ended["error"]
    assert stalled["state"] == "reconnecting" and "stalled" in stalled["error"]
