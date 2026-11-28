# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``video.status``: every source's session state and counters, and no credential (VID-006).

Per source: kind, health, state, session_id, fps, frames, drops, reconnects,
last_frame_age_s, the last verify. Status reads what a capture in ANOTHER process
wrote, so a snapshot left by a process that has exited must read as stopped, not
as a camera that is still streaming.
"""

from __future__ import annotations

import json
import os
import time

from axiom.extensions.builtins.video import connector as vc
from axiom.extensions.builtins.video import state as vs
from axiom.extensions.builtins.video.capture import SessionState, SourceSupervisor

from .fakes import (
    FOREVER,
    FakeDecoder,
    FakeFrameSource,
    RecordingSleep,
    dead_claim,
    registered_kind,
)
from .harness import wait_for


def _rows(result) -> dict[str, dict]:
    assert result.ok, result.errors
    return {row["name"]: row for row in result.value["sources"]}


def test_status_lists_every_declared_source_with_its_session(state_dir, call, fake_ffmpeg):
    with registered_kind(FakeFrameSource()):
        vc.connect(vc.VideoConnector(name="cam-1", source={"kind": "fake"}), state_dir=state_dir)
        vc.connect(
            vc.VideoConnector(name="cam-2", source={"kind": "rtsp", "url": "rtsp://h/ok"}),
            state_dir=state_dir,
        )
        assert vc.verify("cam-1", state_dir=state_dir).ok
        vs.mark_started("cam-1", state_dir)
        sup = SourceSupervisor(
            vc.load("cam-1", state_dir=state_dir),
            state_dir=state_dir,
            decoder=FakeDecoder(default=[FOREVER]),
            poll_s=0.01,
            status_interval_s=0.05,
        )
        sup.start()
        try:
            assert wait_for(
                lambda: (vs.read_live("cam-1", state_dir) or {}).get("frames", 0) > 3, timeout=10
            )
            rows = _rows(call("video.status"))
        finally:
            sup.stop(timeout=10)

    running = rows["cam-1"]
    assert running["kind"] == "fake" and running["started"] is True
    assert running["state"] == "running" and running["health"] == "ready"
    assert running["pid"] == os.getpid() and running["session_id"]
    assert running["frames"] > 3 and running["drops"] == 0 and running["reconnects"] == 0
    assert 0 <= running["last_frame_age_s"] < 5
    assert running["verify"]["ok"] is True

    idle = rows["cam-2"]
    assert idle["kind"] == "rtsp" and idle["state"] == "stopped" and idle["started"] is False
    assert idle["health"] == "unmounted"
    assert idle["source"] == {
        "kind": "rtsp",
        "url": "rtsp://h/ok",
        "transport": "tcp",
        "authenticated": False,
    }


def test_status_of_one_source(state_dir, call):
    vc.connect(
        vc.VideoConnector(name="cam-1", source={"kind": "rtsp", "url": "rtsp://h/ok"}),
        state_dir=state_dir,
    )
    vc.connect(
        vc.VideoConnector(name="cam-2", source={"kind": "rtsp", "url": "rtsp://h/ok"}),
        state_dir=state_dir,
    )
    assert list(_rows(call("video.status", name="cam-2"))) == ["cam-2"]


def test_status_of_a_name_nobody_declared(state_dir, call):
    result = call("video.status", name="ghost")
    assert not result.ok
    assert any("ghost" in e for e in result.errors)


def test_no_sources_is_an_empty_answer_not_an_error(state_dir, call):
    result = call("video.status")
    assert result.ok and result.value["sources"] == []


def test_a_snapshot_left_by_a_dead_process_reads_as_stopped(state_dir, call):
    vc.connect(
        vc.VideoConnector(name="cam-1", source={"kind": "rtsp", "url": "rtsp://h/ok"}),
        state_dir=state_dir,
    )
    dead_claim(vs.claim_item("cam-1", state_dir))
    dead_pid = json.loads(vs.claim_path("cam-1", state_dir).read_text())["pid"]
    vs.write_live(
        "cam-1",
        {
            "name": "cam-1",
            "pid": dead_pid,
            "state": "running",
            "frames": 9,
            "last_frame_at": time.time(),
        },
        state_dir,
    )
    vs.mark_started("cam-1", state_dir)
    row = _rows(call("video.status"))["cam-1"]
    assert row["state"] == "stopped"
    assert row["health"] in ("degraded", "unmounted")
    assert "start" in row["note"], "started but nothing is capturing it says how to bring it back"


def test_a_source_that_is_reconnecting_is_degraded(state_dir, call, fake_ffmpeg):
    vc.connect(
        vc.VideoConnector(name="cam-1", source={"kind": "rtsp", "url": "rtsp://h/ok"}),
        state_dir=state_dir,
    )
    assert vc.verify("cam-1", state_dir=state_dir).ok
    from axiom.infra import pending_claim

    item = vs.claim_item("cam-1", state_dir)
    item.parent.mkdir(parents=True, exist_ok=True)
    held = pending_claim.claim(item)  # held by this process, as a capture in it would
    try:
        vs.write_live(
            "cam-1",
            {
                "name": "cam-1",
                "pid": os.getpid(),
                "state": "reconnecting",
                "frames": 9,
                "reconnects": 3,
                "last_frame_at": time.time() - 30,
            },
            state_dir,
        )
        row = _rows(call("video.status"))["cam-1"]
    finally:
        held.release()
    assert row["state"] == "reconnecting" and row["health"] == "degraded"
    assert row["last_frame_age_s"] >= 30


def test_nothing_status_prints_holds_the_login(state_dir, call, fake_ffmpeg, login):
    """The camera echoes the URL with the login in its error; status must not repeat it."""
    cam = vc.VideoConnector(
        name="cam-1",
        credential_ref=login.ref,
        source={"kind": "rtsp", "url": "rtsp://h/unauthorized"},
    )
    vc.connect(cam, state_dir=state_dir)
    assert not vc.verify("cam-1", state_dir=state_dir).ok

    ref: list = []
    sup = SourceSupervisor(
        cam, state_dir=state_dir, sleep=RecordingSleep(ref, stop_after=1), poll_s=0.01
    )
    ref.append(sup)
    assert sup.run() is SessionState.STOPPED  # the live capture failed the same way, and said so

    result = call("video.status")
    dumped = json.dumps(
        {"value": result.value, "errors": result.errors, "actions": result.actions_taken}
    )
    assert login.password not in dumped and f"{login.username}:" not in dumped
    row = _rows(result)["cam-1"]
    assert row["credential_ref"] == login.ref, "the reference is shown; the value never is"
    assert "401" in row["verify"]["error"] and row["verify"]["fix"]
    assert "401" in (row["error"] or "")
