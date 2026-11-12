# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""VID-012's end-to-end proof, through processes: motion events from a started source, listed.

A source is declared and verified through the CLI, ``motion`` is attached by
editing its connector file (configuration, not a verb: VID-015), and capture is
started. The stand-in ``ffmpeg`` plays a square crossing a still room, so the
capture process opens and closes one episode, and ``instrument video events`` lists it under
one incident key. Then ``connector reload`` changes the attachment while capture
runs, and stopping capture leaves no episode open.
"""

from __future__ import annotations

import subprocess

import pytest

from axiom.extensions.builtins.video import connector as vc
from axiom.extensions.builtins.video import ledger

from .harness import PYTHON, child_env, envelope, run_cli, until

SCENE = {"FAKE_VIDEO_SIZE": "160x90", "FAKE_VIDEO_SCENE": "crossing:10:30"}


def _attach(state_dir, table: str) -> None:
    path = vc.connector_path("cam-1", state_dir=state_dir)
    text = path.read_text(encoding="utf-8")
    head = text.split("[[connector.params.triggers]]")[0].rstrip("\n")
    path.write_text(f"{head}\n\n[[connector.params.triggers]]\n{table}\n", encoding="utf-8")


@pytest.fixture
def started(state_dir, fake_ffmpeg):
    env = {**fake_ffmpeg.env(), **SCENE}
    added = run_cli(
        ["add", "cam-1", "--kind", "rtsp", "--url", "rtsp://127.0.0.1:8554/ok", "--json"],
        state_dir=state_dir,
        env=env,
    )
    assert added.returncode == 0, added.stdout + added.stderr
    _attach(state_dir, 'name = "motion"\ncooldown_s = 1')
    begun = run_cli(["start", "cam-1", "--json"], state_dir=state_dir, env=env)
    assert begun.returncode == 0, begun.stdout + begun.stderr
    yield env
    run_cli(["stop", "cam-1"], state_dir=state_dir, env=env)


def test_motion_events_from_a_started_source_are_listed(state_dir, started):
    until(lambda: [r["kind"] for r in ledger.read(state_dir)] == ["started", "ended"], timeout=40)
    done = run_cli(["events", "cam-1", "--json"], state_dir=state_dir, env=started)
    assert done.returncode == 0, done.stderr
    rows = envelope(done)["value"]["events"]
    assert [r["kind"] for r in rows] == ["started", "ended"]
    assert len({r["incident_key"] for r in rows}) == 1
    assert rows[0]["trigger"] == "motion@0.1.0" and rows[0]["stream"] == "cam-1"

    def motion():
        status = envelope(run_cli(["status", "cam-1", "--json"], state_dir=state_dir, env=started))
        (row,) = status["value"]["sources"]
        (found,) = row["triggers"]
        return found

    # status is the holder's snapshot, written about once a second: it can trail the ledger.
    seen = until(lambda: (m := motion())["events"] == 2 and m, timeout=10)
    assert seen["state"] == "running" and seen["frames"] > 30
    assert seen["health"]["episodes"] == 1


def test_connector_reload_changes_the_attachment_while_capture_runs(state_dir, started):
    until(lambda: (envelope(run_cli(["status", "cam-1", "--json"], state_dir=state_dir, env=started))
                   ["value"]["sources"][0]["triggers"]), timeout=30)  # fmt: skip
    _attach(state_dir, 'name = "motion"\ncooldown_s = 1\nseverity = "warning"')
    done = subprocess.run(
        [PYTHON, "-m", "axiom", "connector", "reload", "cam-1"],  # as a person types it
        env=child_env(state_dir, started),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "cam-1" in done.stdout and "video" in done.stdout
    status = envelope(run_cli(["status", "cam-1", "--json"], state_dir=state_dir, env=started))
    (motion,) = status["value"]["sources"][0]["triggers"]
    assert motion["config"]["severity"] == "warning"


def test_stopping_capture_leaves_no_episode_open(state_dir, started):
    until(lambda: [r["kind"] for r in ledger.read(state_dir)][:1] == ["started"], timeout=30)
    stopped = run_cli(["stop", "cam-1", "--json"], state_dir=state_dir, env=started)
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    kinds = [r["kind"] for r in ledger.read(state_dir)]
    assert kinds.count("started") == kinds.count("ended"), kinds
    episodes = state_dir / "video" / "episodes"
    assert not episodes.exists() or not list(episodes.glob("*.json"))
