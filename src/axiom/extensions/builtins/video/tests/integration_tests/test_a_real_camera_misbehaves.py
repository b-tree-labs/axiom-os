# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A real stream, decoded by real ``ffmpeg``, misbehaving the ways cameras do.

Everything runs through the CLI against :class:`SimulatedCamera` (mediamtx with
a Digest login, ffmpeg ``testsrc``), with the login stored by ``vault set``:

* the camera loses power: the source goes ``reconnecting``/``degraded`` with
  ``reconnects`` climbing, and comes back ``running`` on a new session when the
  camera does;
* each wrong configuration fails at its own rung of ``diagnose`` with its fix;
* the capture is killed with ``SIGKILL``: the real decoder does not outlive it,
  and ``start`` takes the source back;
* the stream is carried over UDP;
* it holds steady for a while (``AXIOM_VIDEO_SOAK_S``, 60 s by default): frames
  keep coming at the cap, no reconnects, no drops, one decoder, memory flat.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

import pytest

from ..harness import (
    alive,
    decoder_pids,
    envelope,
    files_holding,
    run_cli,
    stop_everything,
    vault_set,
    wait_for,
)
from .camera import MISSING, SimulatedCamera, free_port

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


@pytest.fixture
def camera(tmp_path: Path):
    with SimulatedCamera(tmp_path) as cam:
        yield cam


def _cli(state_dir: Path, *argv: str, timeout: float = 120) -> dict:
    return envelope(run_cli([*argv, "--json"], state_dir=state_dir, timeout=timeout))


def _status(state_dir: Path) -> dict:
    return _cli(state_dir, "status", "cam-1")["value"]["sources"][0]


def _add(state_dir: Path, url: str, *extra: str) -> dict:
    return _cli(
        state_dir,
        "add",
        "cam-1",
        "--kind",
        "rtsp",
        "--url",
        url,
        "--credential-ref",
        REF,
        "--fps-cap",
        "5",
        *extra,
    )


def _running(state_dir: Path, *, not_session: str | None = None, min_frames: int = 3):
    def check():
        row = _status(state_dir)
        if row["state"] != "running" or row["frames"] < min_frames:
            return None
        return None if not_session and row["session_id"] == not_session else row

    return check


def _rss_kb(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except OSError:
        return None
    return None


def test_a_camera_that_loses_power_is_reconnected_on_a_new_session(node, camera):
    vault_set(node, "video-cam-1", camera.login())
    assert _add(node, camera.url)["ok"]
    assert _cli(node, "start", "cam-1")["ok"]
    before = wait_for(_running(node), timeout=60, every=1.0)
    assert before, "never reached running"

    camera.power_off()
    lost = wait_for(
        lambda: (
            (r := _status(node))["state"] == "reconnecting"
            and r["reconnects"] >= 1
            and (r["last_frame_age_s"] or 0) >= 3
            and r
        ),
        timeout=40,
        every=1.0,
    )
    assert lost, "the loss was never noticed"
    assert lost["health"] == "degraded"
    assert lost["error"], "status says why it is reconnecting"

    camera.power_on()
    back = wait_for(
        _running(node, not_session=before["session_id"], min_frames=before["frames"] + 3),
        timeout=120,
        every=1.0,
    )
    assert back, "it never came back"
    assert back["health"] == "ready" and back["reconnects"] >= lost["reconnects"]
    assert back["pid"] == before["pid"], (
        "the same capture process reconnected; nothing was restarted"
    )


@pytest.mark.parametrize(
    ("case", "rung", "needle"),
    [
        ("wrong-login", "auth-accepted", "vault set"),
        ("wrong-path", "auth-accepted", "path"),
        ("wrong-port", "describe-answers", "port"),
    ],
)
def test_each_wrong_configuration_fails_at_its_own_rung(node, camera, case, rung, needle):
    if case == "wrong-login":
        vault_set(node, "video-cam-1", f"{camera.user}:not-the-password")
        url = camera.url
    else:
        vault_set(node, "video-cam-1", camera.login())
        url = (
            camera.url.replace("/t", "/elsewhere")
            if case == "wrong-path"
            else f"rtsp://127.0.0.1:{free_port()}/t"
        )
    added = _add(node, url)
    assert added["ok"] is False and added["value"]["active"] is False
    ladder = _cli(node, "diagnose", "cam-1")["value"]
    failed = next(s for s in ladder["steps"] if s["ok"] is False)
    assert failed["step"] == rung, ladder
    assert needle in failed["fix"]
    assert {s["ok"] for s in ladder["steps"][ladder["steps"].index(failed) + 1 :]} <= {None}
    assert camera.password not in repr(ladder)


def _command_lines() -> list[str]:
    """Every process's command line, as ``ps`` shows it to any user of the machine."""
    out = []
    for proc in Path("/proc").iterdir():
        if proc.name.isdigit():
            try:
                out.append(
                    (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
                )
            except OSError:
                continue
    return out


def test_the_vault_login_is_nowhere_on_disk_or_on_a_command_line(node, camera):
    vault_set(node, "video-cam-1", camera.login())
    assert _add(node, camera.url)["ok"]
    assert _cli(node, "start", "cam-1")["ok"]
    assert wait_for(_running(node), timeout=60, every=1.0)
    assert decoder_pids(node, "cam-1"), "a real decoder is running"
    assert [
        c
        for c in _command_lines()
        if camera.password in c and "-i" in c and "rtsp://" in c and "testsrc" not in c
    ] == []
    assert _cli(node, "stop", "cam-1")["ok"]
    assert files_holding(node, camera.password) == []


def test_a_killed_capture_takes_its_real_decoder_with_it(node, camera):
    vault_set(node, "video-cam-1", camera.login())
    assert _add(node, camera.url)["ok"]
    assert _cli(node, "start", "cam-1")["ok"]
    row = wait_for(_running(node), timeout=60, every=1.0)
    decoders = decoder_pids(node, "cam-1")
    assert row and len(decoders) == 1

    os.kill(row["pid"], signal.SIGKILL)
    assert wait_for(lambda: not alive(decoders[0]), timeout=15), (
        "ffmpeg outlived the process reading it"
    )
    stopped = wait_for(lambda: (r := _status(node))["state"] == "stopped" and r, timeout=10)
    assert stopped and "start" in stopped["note"]

    again = _cli(node, "start", "cam-1")
    assert again["ok"] and again["value"]["pid"] != row["pid"]
    assert wait_for(_running(node, not_session=row["session_id"]), timeout=60, every=1.0)
    assert len(decoder_pids(node, "cam-1")) == 1

    assert _cli(node, "stop", "cam-1")["value"]["stopped"]
    assert wait_for(lambda: decoder_pids(node, "cam-1") == [], timeout=15)


def test_the_stream_can_be_carried_over_udp(node, tmp_path):
    with SimulatedCamera(tmp_path, udp=True) as cam:
        vault_set(node, "video-cam-1", cam.login())
        added = _add(node, cam.url, "--transport", "udp")
        assert added["ok"], added
        assert _status(node)["source"]["transport"] == "udp"
        assert _cli(node, "start", "cam-1")["ok"]
        row = wait_for(_running(node, min_frames=10), timeout=60, every=1.0)
        assert row, _status(node)
        assert _cli(node, "stop", "cam-1")["ok"]


@pytest.mark.slow
def test_it_holds_steady_over_a_long_run(node, camera):
    soak_s = float(os.environ.get("AXIOM_VIDEO_SOAK_S", "60"))
    vault_set(node, "video-cam-1", camera.login())
    assert _add(node, camera.url)["ok"]
    assert _cli(node, "start", "cam-1")["ok"]
    first = wait_for(_running(node, min_frames=20), timeout=60, every=1.0)
    assert first
    pid, rss_start = first["pid"], _rss_kb(first["pid"])

    samples = []
    deadline = time.monotonic() + soak_s
    while time.monotonic() < deadline:
        time.sleep(min(5.0, max(0.5, deadline - time.monotonic())))
        samples.append((_status(node), len(decoder_pids(node, "cam-1"))))
    last = samples[-1][0]

    frames = [s["frames"] for s, _ in samples]
    assert frames == sorted(frames) and frames[-1] > frames[0], "frames kept coming"
    assert all(
        s["state"] == "running" and s["session_id"] == first["session_id"] for s, _ in samples
    )
    assert {n for _, n in samples} == {1}, "exactly one decoder throughout"
    assert last["reconnects"] == 0 and last["drops"] == 0
    assert 4.0 <= last["fps"] <= 6.0, last["fps"]
    expected = 5 * soak_s
    assert last["frames"] - first["frames"] >= 0.9 * expected, "no frames went missing at the cap"
    if rss_start is not None:
        growth_mb = ((_rss_kb(pid) or rss_start) - rss_start) / 1024
        assert growth_mb < 25, f"the capture process grew {growth_mb:.1f} MB"
    assert last["pid"] == pid
