# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""PRD-video §8 Phase 1 acceptance: a simulated camera is added, verified, captured and
observed through the CLI, with real ``ffmpeg`` decoding a real RTSP stream.

The camera is ``ffmpeg`` publishing ``testsrc`` (H.264, a keyframe every second)
to a local RTSP server that demands a login. ``ffmpeg`` alone cannot serve RTSP
(``-rtsp_flags listen`` is an input flag), so the server is ``mediamtx``; without
both binaries the test is skipped and says which one is missing.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from axiom.extensions.builtins.video import state as vs

from ..harness import envelope, run_cli, stop_everything, wait_for

pytestmark = pytest.mark.integration

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
MEDIAMTX = shutil.which("mediamtx")

needs_binaries = pytest.mark.skipif(
    not (FFMPEG and FFPROBE and MEDIAMTX),
    reason=f"needs ffmpeg, ffprobe and mediamtx on PATH (ffmpeg={FFMPEG}, ffprobe={FFPROBE}, mediamtx={MEDIAMTX})",
)

MEDIAMTX_CONFIG = """\
logLevel: warn
rtspAddress: 127.0.0.1:{port}
rtspTransports: [tcp]
rtspAuthMethods: [digest]
rtmp: no
hls: no
webrtc: no
srt: no
moq: no
api: no
metrics: no
pprof: no
playback: no
authInternalUsers:
  - user: {user}
    pass: {password}
    ips: []
    permissions:
      - action: publish
      - action: read
paths:
  all_others:
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def camera(tmp_path: Path):
    """A camera at ``rtsp://127.0.0.1:<port>/t`` that wants ``(user, password)``."""
    port, user, password = _free_port(), "viewer", f"pw-{uuid.uuid4().hex[:12]}"
    config = tmp_path / "mediamtx.yml"
    config.write_text(MEDIAMTX_CONFIG.format(port=port, user=user, password=password))
    server = subprocess.Popen(
        [MEDIAMTX, str(config)], cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    publisher = None
    try:
        assert wait_for(lambda: _listening(port), timeout=10), "mediamtx did not start"
        publisher = subprocess.Popen(
            [
                FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-re",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=640x360:rate=10",
                "-c:v",
                "libx264",
                "-tune",
                "zerolatency",
                "-g",
                "10",
                "-pix_fmt",
                "yuv420p",
                "-f",
                "rtsp",
                "-rtsp_transport",
                "tcp",
                f"rtsp://{user}:{password}@127.0.0.1:{port}/t",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(2.0)  # the publisher announces and sends its first keyframe
        yield {"url": f"rtsp://127.0.0.1:{port}/t", "user": user, "password": password}
    finally:
        for proc in (publisher, server):
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


@needs_binaries
def test_add_verify_start_observe_stop(tmp_path: Path, camera, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    var = f"VIDEO_IT_LOGIN_{uuid.uuid4().hex[:8].upper()}"
    env = {var: f"{camera['user']}:{camera['password']}"}
    for key in ("AXIOM_VIDEO_FFMPEG", "AXIOM_VIDEO_FFPROBE"):
        monkeypatch.delenv(key, raising=False)
    try:
        added = run_cli(
            [
                "add",
                "cam-1",
                "--kind",
                "rtsp",
                "--url",
                camera["url"],
                "--credential-ref",
                f"env://{var}",
                "--fps-cap",
                "5",
                "--json",
            ],
            state_dir=state_dir,
            env=env,
            timeout=120,
        )
        assert added.returncode == 0, added.stdout + added.stderr
        report = envelope(added)["value"]["verify"]["report"]
        assert (report["codec"], report["width"], report["height"]) == ("h264", 640, 360)

        diagnosed = run_cli(
            ["diagnose", "cam-1", "--json"], state_dir=state_dir, env=env, timeout=120
        )
        assert diagnosed.returncode == 0, diagnosed.stdout

        started = run_cli(["start", "cam-1", "--json"], state_dir=state_dir, env=env, timeout=120)
        assert started.returncode == 0, started.stdout + started.stderr

        def status():
            return envelope(run_cli(["status", "cam-1", "--json"], state_dir=state_dir, env=env))[
                "value"
            ]["sources"][0]

        first = wait_for(lambda: (s := status())["frames"] >= 5 and s, timeout=60, every=1.0)
        assert first and first["state"] == "running", first
        time.sleep(2.0)
        later = status()
        assert later["frames"] > first["frames"], "counters advance while it is captured"
        assert later["session_id"] == first["session_id"]
        assert later["last_frame_age_s"] < 5
        assert 3.0 <= later["fps"] <= 7.0, later["fps"]

        wrong = run_cli(
            [
                "add",
                "cam-2",
                "--kind",
                "rtsp",
                "--url",
                camera["url"],
                "--credential-ref",
                f"env://{var}_WRONG",
                "--json",
            ],
            state_dir=state_dir,
            env={**env, f"{var}_WRONG": f"{camera['user']}:nope"},
            timeout=120,
        )
        assert wrong.returncode == 1 and envelope(wrong)["value"]["verify"]["code"] == "auth-failed"

        stopped = run_cli(["stop", "cam-1", "--json"], state_dir=state_dir, env=env, timeout=60)
        assert envelope(stopped)["value"]["stopped"] is True
        assert vs.claim_holder("cam-1", state_dir) is None

        everything = " ".join(
            p.read_text(errors="replace") for p in state_dir.rglob("*") if p.is_file()
        )
        assert camera["password"] not in everything
    finally:
        stop_everything(state_dir)
