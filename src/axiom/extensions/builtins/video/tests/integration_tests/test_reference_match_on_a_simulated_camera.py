# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""PRD-video §8 Phase 3 acceptance, on a real stream: ``reference-match`` reports a door that
opens and closes as one change each time, with heartbeats between, and the events are
listed by ``instrument video events``.

The camera is :class:`SimulatedCamera` (``scene="door"``: the room of
``scenes.door_scene`` drawn by ffmpeg, its door closed for 10 s and open for 10 s),
published as H.264 over RTSP with a Digest login. The reference set is written from
the same geometry, never from the stream, so the test also shows that pictures taken
from a description survive a real encoder. A set at a working size the stream does
not fit leaves the trigger ``failed`` with its fix.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.extensions.builtins.video import connector as vc
from axiom.extensions.builtins.video import ledger

from ..harness import envelope, run_cli, stop_everything, until, vault_set
from ..scenes import GUARDS, door_reference_set
from .camera import MISSING, SimulatedCamera

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(bool(MISSING), reason=f"needs {', '.join(MISSING)} on PATH"),
]

REF = "foreign://video-cam-1"
#: Fast, so a test sees a change in seconds: the door changes every 10 s.
SETTINGS = "sample_s = 0.5\ndwell_s = 2.0\nleave_s = 1.0\nheartbeat_s = 4.0\n"


@pytest.fixture
def node(state_dir: Path, file_vault, monkeypatch):
    for key in ("AXIOM_VIDEO_FFMPEG", "AXIOM_VIDEO_FFPROBE"):
        monkeypatch.delenv(key, raising=False)
    yield state_dir
    stop_everything(state_dir)


def _cli(state_dir: Path, *argv: str) -> dict:
    return envelope(run_cli([*argv, "--json"], state_dir=state_dir, timeout=120))


def _watch(state_dir: Path, camera: SimulatedCamera, reference_set: Path) -> None:
    """Declare the camera with its vault login, attach reference-match, start capturing."""
    vault_set(state_dir, "video-cam-1", camera.login())
    added = _cli(
        state_dir, "add", "cam-1", "--kind", "rtsp", "--url", camera.url, "--credential-ref", REF
    )
    assert added["ok"], added
    path = vc.connector_path("cam-1", state_dir=state_dir)
    path.write_text(
        path.read_text(encoding="utf-8")
        + '\n[[connector.params.triggers]]\nname = "reference-match"\n'
        + f'reference_set = "{reference_set}"\n'
        + SETTINGS,
        encoding="utf-8",
    )
    assert _cli(state_dir, "start", "cam-1")["ok"]


def _trigger(state_dir: Path) -> dict:
    (row,) = _cli(state_dir, "status", "cam-1")["value"]["sources"]
    return next((t for t in row["triggers"] if t["name"] == "reference-match"), {})


def _ended(state_dir: Path) -> list[dict]:
    return [r for r in ledger.read(state_dir) if r["kind"] == "ended"]


def test_a_door_that_opens_and_closes_is_one_change_each_time(node, tmp_path):
    refs = door_reference_set(
        tmp_path / "refs",
        size=(640, 360),
        working=(320, 180),
        masks=(),
        guards={**GUARDS, "view": 0.8},
    )
    with SimulatedCamera(tmp_path, scene="door") as camera:
        _watch(node, camera, refs.path)
        until(lambda: len(_ended(node)) >= 2, timeout=90)
        rows = _cli(node, "events", "cam-1")["value"]["events"]
        watched = _trigger(node)
    assert watched["state"] == "running" and watched["errors"] == 0, watched
    assert {r["trigger"] for r in rows} == {"reference-match@0.1.0"}
    assert {r["detail"]["region"] for r in rows} == {"door"}
    first = rows[0]
    assert first["kind"] == "occurred" and first["detail"]["first"] is True
    assert first["detail"]["state"] in ("closed", "open")
    ended = [r for r in rows if r["kind"] == "ended"]
    started = {r["incident_key"]: r for r in rows if r["kind"] == "started"}
    for row in ended:
        assert row["incident_key"] in started, "each change is one started and one ended"
        assert row["detail"]["from"] != row["detail"]["to"], "the door changed every time"
        assert row["detail"]["returned"] is False
    labels = [first["detail"]["state"], *(r["detail"]["to"] for r in ended)]
    assert all(a != b for a, b in zip(labels, labels[1:], strict=False)), labels
    beats = [r for r in rows if r["kind"] == "occurred" and r["detail"].get("heartbeat")]
    assert beats, "a heartbeat every 4 s while the door holds its label"
    region = watched["health"]["regions"]["door"]
    assert region["state"] in ("closed", "open", "changing") and region["changes"] >= 2


def test_a_set_the_stream_does_not_fit_leaves_the_trigger_failed_with_its_fix(node, tmp_path):
    refs = door_reference_set(tmp_path / "refs", size=(600, 360), working=(300, 180), masks=())
    with SimulatedCamera(tmp_path, scene="door") as camera:
        _watch(node, camera, refs.path)
        failed = until(lambda: (t := _trigger(node)).get("state") == "failed" and t, timeout=60)
        (row,) = _cli(node, "status", "cam-1")["value"]["sources"]
    assert "640x360" in failed["last_error"] and "300x180" in failed["last_error"]
    assert failed["fix"]
    assert row["health"] == "degraded" and "reference-match" in row["note"]
    assert ledger.read(node) == []
