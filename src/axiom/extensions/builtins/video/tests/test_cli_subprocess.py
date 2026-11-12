# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Every ``axi instrument video`` verb, run as a subprocess the way a person runs it (spec-video §Tests).

The verbs go through ``invoke_capability`` in a fresh process on this checkout's
source with its own state directory. ``ffmpeg`` and ``ffprobe`` are the stand-ins
from fake_ffmpeg.py and the camera, where one is needed, is fake_rtsp.py, so no
binary is required; the live-camera version of this is the integration test.
"""

from __future__ import annotations

import json
import os

import pytest

from axiom.extensions.builtins.video import state as vs

from .fake_rtsp import FakeCamera
from .harness import envelope, run_cli, wait_for

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="the stand-in tools are POSIX shell wrappers"
)


def _add(state_dir, name="cam-1", url="rtsp://cam.example.org/ok", *extra):
    return run_cli(
        ["add", name, "--kind", "rtsp", "--url", url, *extra, "--json"], state_dir=state_dir
    )


def test_add_writes_the_connector_and_runs_verify(state_dir, fake_ffmpeg, login):
    result = _add(
        state_dir,
        "cam-1",
        "rtsp://cam.example.org/ok",
        "--credential-ref",
        login.ref,
        "--fps-cap",
        "5",
    )
    assert result.returncode == 0, result.stderr
    body = envelope(result)
    assert body["ok"] is True
    assert body["value"]["verify"]["ok"] is True
    assert body["value"]["verify"]["report"]["codec"] == "h264"
    text = (state_dir / "plinth" / "connectors" / "cam-1.toml").read_text()
    assert 'kind = "video"' in text and "fps_cap = 5" in text
    assert login.password not in result.stdout + result.stderr + text


def test_add_refuses_a_url_with_a_login_and_says_where_it_goes(state_dir, fake_ffmpeg):
    result = _add(state_dir, "cam-1", "rtsp://admin:hunter2@cam.example.org/s")
    assert result.returncode == 1
    out = result.stdout + result.stderr
    assert "vault set" in out and "hunter2" not in out
    assert not (state_dir / "plinth" / "connectors" / "cam-1.toml").exists()


def test_add_a_source_that_does_not_verify_declares_it_inactive(state_dir, fake_ffmpeg):
    result = _add(state_dir, "cam-1", "rtsp://cam.example.org/missing")
    assert result.returncode == 1
    body = envelope(result)
    assert body["value"]["verify"]["ok"] is False and body["value"]["active"] is False
    assert (state_dir / "plinth" / "connectors" / "cam-1.toml").exists()


def test_add_a_name_that_is_taken(state_dir, fake_ffmpeg):
    assert _add(state_dir).returncode == 0
    result = _add(state_dir)
    assert result.returncode == 1 and "cam-1" in result.stdout + result.stderr


def test_verify(state_dir, fake_ffmpeg):
    _add(state_dir)
    result = run_cli(["verify", "cam-1", "--json"], state_dir=state_dir)
    assert result.returncode == 0, result.stderr
    report = envelope(result)["value"]["report"]
    assert set(report) >= {"codec", "width", "height", "fps", "latency_ms"}


def test_verify_human_output_names_the_numbers(state_dir, fake_ffmpeg):
    _add(state_dir)
    result = run_cli(["verify", "cam-1"], state_dir=state_dir)
    assert result.returncode == 0, result.stderr
    assert "h264" in result.stdout and "64x48" in result.stdout


def test_list_and_list_kinds(state_dir, fake_ffmpeg):
    _add(state_dir)
    listed = run_cli(["list"], state_dir=state_dir)
    assert listed.returncode == 0, listed.stderr
    assert "cam-1" in listed.stdout and "rtsp" in listed.stdout
    kinds = envelope(run_cli(["list", "--kinds", "--json"], state_dir=state_dir))
    assert {k["kind"]: k["replayable"] for k in kinds["value"]["kinds"]}["rtsp"] is False


def test_status_json_holds_no_credential(state_dir, fake_ffmpeg, login):
    _add(state_dir, "cam-1", "rtsp://cam.example.org/unauthorized", "--credential-ref", login.ref)
    result = run_cli(["status", "--json"], state_dir=state_dir)
    assert result.returncode == 0, result.stderr
    assert login.password not in result.stdout + result.stderr
    row = envelope(result)["value"]["sources"][0]
    assert row["name"] == "cam-1" and row["health"] == "failed"


def test_status_human_output(state_dir, fake_ffmpeg):
    _add(state_dir)
    result = run_cli(["status", "cam-1"], state_dir=state_dir)
    assert result.returncode == 0, result.stderr
    assert "cam-1" in result.stdout and "stopped" in result.stdout


def test_start_captures_and_stop_releases(state_dir, fake_ffmpeg, login, no_leftovers):
    _add(
        state_dir,
        "cam-1",
        "rtsp://cam.example.org/ok",
        "--credential-ref",
        login.ref,
        "--fps-cap",
        "5",
    )
    started = run_cli(["start", "cam-1", "--json"], state_dir=state_dir)
    assert started.returncode == 0, started.stderr + started.stdout
    pid = envelope(started)["value"]["pid"]
    assert pid and pid != os.getpid()

    def frames():
        row = envelope(run_cli(["status", "cam-1", "--json"], state_dir=state_dir))["value"][
            "sources"
        ][0]
        return row if row["state"] == "running" and row["frames"] >= 2 else None

    row = wait_for(frames, timeout=30, every=0.5)
    assert row, "capture never reached running with frames"
    assert row["pid"] == pid and row["session_id"]

    again = envelope(run_cli(["start", "cam-1", "--json"], state_dir=state_dir))
    assert (
        again["ok"] and again["value"]["already_running"] is True and again["value"]["pid"] == pid
    )

    stopped = run_cli(["stop", "cam-1", "--json"], state_dir=state_dir)
    assert stopped.returncode == 0, stopped.stderr
    assert envelope(stopped)["value"]["stopped"] is True
    assert vs.claim_holder("cam-1", state_dir) is None
    assert not vs.is_started("cam-1", state_dir)

    # The login reached ffmpeg through its pipe, never its argv, and is nowhere on disk.
    assert not any(login.password in " ".join(argv) for argv in fake_ffmpeg.argv())
    assert any(login.password in seen["url"] for seen in fake_ffmpeg.inputs())
    on_disk = [p for p in state_dir.rglob("*") if p.is_file()]
    assert not [p for p in on_disk if login.password in p.read_text(errors="replace")]


def test_start_an_unverified_source_is_refused_with_the_fix(state_dir, fake_ffmpeg, no_leftovers):
    _add(state_dir, "cam-1", "rtsp://cam.example.org/missing")
    result = run_cli(["start", "cam-1", "--json"], state_dir=state_dir)
    assert result.returncode == 1
    assert "instrument video verify" in result.stdout + result.stderr
    assert vs.claim_holder("cam-1", state_dir) is None


def test_start_without_ffmpeg_names_the_install(state_dir, fake_ffmpeg, no_leftovers, tmp_path):
    _add(state_dir)
    env = {
        "AXIOM_VIDEO_FFMPEG": str(tmp_path / "nowhere"),
        "AXIOM_VIDEO_FFPROBE": str(tmp_path / "nowhere"),
    }
    result = run_cli(["start", "cam-1", "--json"], state_dir=state_dir, env=env)
    assert result.returncode == 1
    assert "install" in (result.stdout + result.stderr).lower()


def test_stop_a_source_that_is_not_running(state_dir, fake_ffmpeg):
    _add(state_dir)
    result = run_cli(["stop", "cam-1", "--json"], state_dir=state_dir)
    assert result.returncode == 0, result.stderr
    assert envelope(result)["value"]["was_running"] is False


def test_diagnose_climbs_the_ladder(state_dir, fake_ffmpeg):
    with FakeCamera() as cam:
        _add(state_dir, "cam-1", cam.url("/t"))
        ok = run_cli(["diagnose", "cam-1"], state_dir=state_dir)
        assert ok.returncode == 0, ok.stdout + ok.stderr
        for rung in ("ffmpeg", "describe-answers", "verify"):
            assert rung in ok.stdout

        _add(state_dir, "cam-2", cam.url("/elsewhere"))
        bad = run_cli(["diagnose", "cam-2", "--json"], state_dir=state_dir)
    assert bad.returncode == 1
    body = envelope(bad)
    assert body["value"]["fix"] and "path" in body["value"]["fix"]
    assert json.dumps(body).count('"ok": null') >= 1, "rungs after the failure are skipped"
