# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``FfmpegDecoder``: the subprocess side of a session (spec-video §Design).

It runs the capture command, reads ``width*height*3`` bytes per frame from
stdout, reports EOF, counts a trailing half frame, raises on a stall, and kills
the process on close. The process is registered (a process the platform cannot
name is one it cannot stop) with its command REDACTED, because the command line
holds the login.
"""

from __future__ import annotations

import time

import pytest

from axiom.extensions.builtins.secrets import Secret
from axiom.extensions.builtins.video.capture import FfmpegDecoder, StallTimeout
from axiom.extensions.builtins.video.ffmpeg import FFmpegTool
from axiom.extensions.builtins.video.sources.rtsp import build
from axiom.infra import process_registry

FRAME = 64 * 48 * 3


def _open(state_dir, url="rtsp://cam.example.org/ok", secret=b"viewer:pw-decoder-test", fps_cap=50):
    source = build({"url": url}, Secret(value=secret))
    return FfmpegDecoder(FFmpegTool.locate(), state_dir=state_dir, name="cam-1").open(
        source, width=64, height=48, fps_cap=fps_cap
    )


def test_frames_come_off_the_pipe_whole(state_dir, fake_ffmpeg):
    session = _open(state_dir)
    try:
        frames = [session.read(timeout=10) for _ in range(3)]
    finally:
        session.close()
    assert [len(f) for f in frames] == [FRAME] * 3
    assert frames[0] != frames[1], "consecutive frames, not one frame read three times"


def test_eof_when_ffmpeg_exits(state_dir, fake_ffmpeg, monkeypatch):
    monkeypatch.setenv("FAKE_VIDEO_FRAMES", "2")
    session = _open(state_dir)
    try:
        assert session.read(timeout=10) and session.read(timeout=10)
        assert session.read(timeout=10) is None
        assert session.partial_frames == 0
    finally:
        session.close()


def test_a_trailing_half_frame_is_counted_not_delivered(state_dir, fake_ffmpeg, monkeypatch):
    monkeypatch.setenv("FAKE_VIDEO_FRAMES", "1")
    monkeypatch.setenv("FAKE_VIDEO_SIZE", "64x40")  # writes fewer bytes than one 64x48 frame
    session = _open(state_dir)
    try:
        assert session.read(timeout=10) is None
        assert session.partial_frames == 1
    finally:
        session.close()


def test_a_stall_raises(state_dir, fake_ffmpeg, monkeypatch):
    monkeypatch.setenv("FAKE_VIDEO_STALL_AFTER", "1")
    session = _open(state_dir)
    try:
        assert session.read(timeout=10)
        started = time.monotonic()
        with pytest.raises(StallTimeout):
            session.read(timeout=0.5)
        assert time.monotonic() - started < 5
    finally:
        session.close()


def test_close_kills_ffmpeg_and_drops_its_record(state_dir, fake_ffmpeg):
    session = _open(state_dir)
    session.read(timeout=10)
    pid = session.pid
    assert pid and process_registry.probe(pid) is not None
    session.close()
    deadline = time.monotonic() + 10
    while process_registry.probe(pid) is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert process_registry.probe(pid) is None
    assert all(r.pid != pid for r in process_registry.ProcessRegistry(state_dir).records())


def test_the_recorded_command_holds_no_login(state_dir, fake_ffmpeg):
    session = _open(state_dir, secret=b"viewer:pw-in-argv-only")
    try:
        session.read(timeout=10)
        (record,) = [
            r for r in process_registry.ProcessRegistry(state_dir).records() if r.pid == session.pid
        ]
        assert "pw-in-argv-only" not in record.command and "viewer:" not in record.command
        assert "cam.example.org/ok" in record.command
        assert record.extension == "video"
    finally:
        session.close()
    assert not any("pw-in-argv-only" in " ".join(argv) for argv in fake_ffmpeg.argv())
    assert any("pw-in-argv-only" in seen["url"] for seen in fake_ffmpeg.inputs()), (
        "the child got it, from its pipe"
    )


def test_the_running_decoders_command_line_holds_no_login(state_dir, fake_ffmpeg):
    """What `ps` shows any user of the machine."""
    from pathlib import Path

    session = _open(state_dir, secret=b"viewer:pw-not-for-ps")
    try:
        session.read(timeout=10)
        proc = Path(f"/proc/{session.pid}/cmdline")
        if proc.exists():
            cmdline = proc.read_bytes()
        else:  # no /proc (macOS): ask ps, which is what another user of the machine would run
            import subprocess

            cmdline = subprocess.run(
                ["ps", "-ww", "-o", "command=", "-p", str(session.pid)],
                capture_output=True,
                check=True,
            ).stdout
            assert cmdline.strip(), "ps saw the decoder"
        assert b"pw-not-for-ps" not in cmdline and b"viewer:" not in cmdline
    finally:
        session.close()


def test_what_ffmpeg_said_is_scrubbed(state_dir, fake_ffmpeg):
    session = _open(
        state_dir, url="rtsp://cam.example.org/unauthorized", secret=b"viewer:pw-echoed"
    )
    try:
        assert session.read(timeout=10) is None
        said = session.error()
    finally:
        session.close()
    assert "401" in said
    assert "pw-echoed" not in said and "viewer:" not in said


def test_closing_a_session_wakes_a_read_that_is_waiting(state_dir, fake_ffmpeg, monkeypatch):
    """``stop`` closes the session from another thread while capture waits for a frame.

    That read must return at once, not wait out the stall timeout: before, every
    stop of a running source took that long (5 s or more) and logged "the stream
    stalled" for a stream that was fine.
    """
    import threading

    monkeypatch.setenv("FAKE_VIDEO_STALL_AFTER", "1")
    session = _open(state_dir)
    assert session.read(timeout=10)
    got: list = []
    reader = threading.Thread(target=lambda: got.append(_timed(session.read, 30)))
    reader.start()
    time.sleep(0.3)
    session.close()
    reader.join(timeout=10)
    assert got, "the read was still waiting 10 s after the session was closed"
    (result, took) = got[0]
    assert result is None and took < 2.0, f"read waited {took:.1f} s after close"


def _timed(fn, timeout):
    t0 = time.monotonic()
    try:
        out = fn(timeout)
    except StallTimeout as exc:
        out = exc
    return out, time.monotonic() - t0
