# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""PRD-video §8 Phase 2 acceptance, on a real stream: ``motion`` opens and closes an episode
when something crosses the camera's view, stays silent on a still room's sensor noise,
and each episode is in the ledger under one incident key, listed by ``instrument video events``.

The camera is :class:`SimulatedCamera` (mediamtx with a Digest login; ffmpeg
publishing an H.264 lavfi scene), the login is stored with ``vault set``, and
``motion`` is attached by writing the source's connector file (VID-015).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from axiom.extensions.builtins.video import connector as vc
from axiom.extensions.builtins.video import ledger

from ..harness import envelope, run_cli, stop_everything, until, vault_set
from .camera import MISSING, SimulatedCamera

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(bool(MISSING), reason=f"needs {', '.join(MISSING)} on PATH"),
]

REF = "foreign://video-cam-1"


@pytest.fixture
def node(state_dir: Path, file_vault, monkeypatch):
    for key in ("AXIOM_VIDEO_FFMPEG", "AXIOM_VIDEO_FFPROBE"):
        monkeypatch.delenv(key, raising=False)
    yield state_dir
    stop_everything(state_dir)


def _cli(state_dir: Path, *argv: str) -> dict:
    return envelope(run_cli([*argv, "--json"], state_dir=state_dir, timeout=120))


def _watch(state_dir: Path, camera: SimulatedCamera) -> None:
    """Declare the camera with its vault login, attach motion, start capturing."""
    vault_set(state_dir, "video-cam-1", camera.login())
    added = _cli(
        state_dir, "add", "cam-1", "--kind", "rtsp", "--url", camera.url, "--credential-ref", REF
    )
    assert added["ok"], added
    path = vc.connector_path("cam-1", state_dir=state_dir)
    path.write_text(
        path.read_text(encoding="utf-8")
        + '\n[[connector.params.triggers]]\nname = "motion"\ncooldown_s = 2\n',
        encoding="utf-8",
    )
    assert _cli(state_dir, "start", "cam-1")["ok"]


def _motion(state_dir: Path) -> dict:
    (row,) = _cli(state_dir, "status", "cam-1")["value"]["sources"]
    triggers = {t["name"]: t for t in row["triggers"]}
    return triggers.get("motion") or {}


def test_something_crossing_the_view_is_one_episode_per_crossing(node, tmp_path):
    with SimulatedCamera(tmp_path, scene="crossing") as camera:
        _watch(node, camera)
        until(
            lambda: [r["kind"] for r in ledger.read(node)][:2] == ["started", "ended"], timeout=60
        )
        rows = _cli(node, "events", "cam-1")["value"]["events"]
    first, second = rows[0], rows[1]
    assert (first["kind"], second["kind"]) == ("started", "ended")
    assert first["incident_key"] == second["incident_key"]
    assert first["trigger"] == "motion@0.1.0" and first["detail"]["method"] == "frame_diff"
    # A crossing lasts three seconds; with a two-second cooldown an episode is seconds long, not one frame.
    from datetime import datetime

    length = (
        datetime.fromisoformat(second["ts"]) - datetime.fromisoformat(first["ts"])
    ).total_seconds()
    assert 2.0 <= length <= 12.0, length


def test_a_still_room_is_silent(node, tmp_path):
    with SimulatedCamera(tmp_path, scene="still-noise") as camera:
        _watch(node, camera)
        until(lambda: _motion(node).get("frames", 0) > 0, timeout=30)
        time.sleep(12)
        watched = _motion(node)
    assert watched["frames"] >= 80, watched
    assert watched["state"] == "running" and watched["errors"] == 0
    assert ledger.read(node) == [], "sensor noise is not motion"
