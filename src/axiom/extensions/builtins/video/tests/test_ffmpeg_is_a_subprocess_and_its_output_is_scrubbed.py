# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Decoding is an ``ffmpeg`` subprocess, and nothing it prints leaves with a login in it.

ADR-240 D2: no video library is imported into the Axiom process. spec-video §Design:
``ffmpeg -nostdin -loglevel error <input args> -vf fps=<cap> -f rawvideo -pix_fmt
rgb24 pipe:1``; the command line is never logged. ``ffmpeg`` echoes its input URL,
login included, in its own errors, so every error the extension surfaces is
scrubbed first. VID-008: a missing ``ffmpeg`` and each failure class name a fix.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.video.ffmpeg import (
    FFMPEG_ENV,
    FFPROBE_ENV,
    SECRET_INPUT,
    FFmpegMissing,
    FFmpegTool,
    classify_failure,
    install_hint,
    redact_command,
    scrub,
)
from axiom.extensions.builtins.video.sources import VideoSourceError

INPUT = ["-rtsp_transport", "tcp", "-i", "rtsp://viewer:s3cret-pw@cam.example.org/stream1"]


# ---- locating ----------------------------------------------------------------


def test_the_override_names_the_binaries(fake_ffmpeg):
    tool = FFmpegTool.locate()
    assert tool.ffmpeg == str(fake_ffmpeg.ffmpeg)
    assert tool.ffprobe == str(fake_ffmpeg.ffprobe)
    assert tool.available
    assert "fake" in tool.version()


def test_an_override_pointing_nowhere_is_missing_not_a_fallback_to_path(no_ffmpeg):
    tool = FFmpegTool.locate()
    assert not tool.available
    with pytest.raises(FFmpegMissing) as info:
        tool.require()
    assert info.value.code == "ffmpeg-missing"
    assert info.value.fix == install_hint()


def test_without_an_override_it_is_looked_up_on_path(monkeypatch, tmp_path):
    monkeypatch.delenv(FFMPEG_ENV, raising=False)
    monkeypatch.delenv(FFPROBE_ENV, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert FFmpegTool.locate().available is False


@pytest.mark.parametrize(
    ("platform", "needle"),
    [
        ("linux", "apt install ffmpeg"),
        ("darwin", "brew install ffmpeg"),
        ("win32", "winget install"),
    ],
)
def test_the_install_hint_names_the_package_manager(platform, needle):
    hint = install_hint(platform)
    assert needle in hint
    assert FFMPEG_ENV in hint, "the hint also says how to point at an ffmpeg elsewhere"


def test_decoders_are_read_from_ffmpeg(fake_ffmpeg, monkeypatch):
    monkeypatch.setenv("FAKE_VIDEO_DECODERS", "h264,mjpeg")
    assert FFmpegTool.locate().decoders() == frozenset({"h264", "mjpeg"})


# ---- the commands ------------------------------------------------------------


def test_the_capture_command_is_the_same_after_the_input_for_every_kind():
    tool = FFmpegTool(ffmpeg="/usr/bin/ffmpeg", ffprobe="/usr/bin/ffprobe")
    assert tool.capture_command(INPUT, fps_cap=10) == [
        "/usr/bin/ffmpeg",
        "-nostdin",
        "-loglevel",
        "error",
        *INPUT,
        "-vf",
        "fps=10",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "pipe:1",
    ]


def test_the_grab_command_asks_for_exactly_one_frame():
    cmd = FFmpegTool(ffmpeg="ffmpeg", ffprobe="ffprobe").grab_command(INPUT)
    assert cmd[cmd.index("-frames:v") + 1] == "1"
    assert cmd[-5:] == ["-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]


def test_the_probe_command_asks_ffprobe_for_json():
    cmd = FFmpegTool(ffmpeg="ffmpeg", ffprobe="ffprobe").probe_command(INPUT)
    assert cmd[0] == "ffprobe" and "json" in cmd and INPUT[-1] in cmd


def test_a_command_is_redacted_before_it_is_recorded_anywhere():
    cmd = FFmpegTool(ffmpeg="ffmpeg", ffprobe="ffprobe").capture_command(INPUT, fps_cap=5)
    shown = " ".join(redact_command(cmd, ["s3cret-pw"]))
    assert "s3cret-pw" not in shown and "viewer:" not in shown
    assert "cam.example.org/stream1" in shown


# ---- scrubbing and classifying -----------------------------------------------


def test_scrub_removes_userinfo_and_any_known_secret():
    text = "rtsp://viewer:s3cret-pw@cam.example.org/stream1: Server returned 401; also s3cret-pw"
    clean = scrub(text, ["s3cret-pw"])
    assert "s3cret-pw" not in clean and "viewer:" not in clean
    assert "cam.example.org/stream1" in clean and "401" in clean


def test_scrub_removes_a_percent_encoded_secret_too():
    clean = scrub("rtsp://u%40x:p%3Aw@h/s failed", ["p:w"])
    assert "p%3Aw" not in clean and "u%40x" not in clean


@pytest.mark.parametrize(
    ("stderr", "code", "needle"),
    [
        ("x: Server returned 401 Unauthorized (authorization failed)", "auth-failed", "vault"),
        ("x: Server returned 404 Not Found", "not-found", "path"),
        ("Connection to tcp://h:554 failed: Connection refused", "unreachable", "network"),
        ("x: Connection timed out", "unreachable", "network"),
        (
            "Failed to resolve hostname cam.example.org: Name or service not known",
            "unreachable",
            "host",
        ),
        ("x: Invalid data found when processing input", "codec-unsupported", "codec"),
        ("Decoder (codec av1) not found for input stream #0:0", "codec-unsupported", "codec"),
        (
            "[rtsp @ 0x1] method SETUP failed: 461 Unsupported Transport",
            "transport-unsupported",
            "transport",
        ),
        ("something nobody anticipated", "error", "diagnose"),
    ],
)
def test_each_failure_class_names_its_fix(stderr, code, needle):
    got_code, fix = classify_failure(stderr)
    assert got_code == code
    assert needle in fix.lower()


# ---- probing through the stand-in tools ---------------------------------------


def test_probe_reports_codec_size_rate_and_latency(fake_ffmpeg, monkeypatch):
    monkeypatch.setenv("FAKE_VIDEO_SIZE", "32x16")
    monkeypatch.setenv("FAKE_VIDEO_FPS", "15")
    report = FFmpegTool.locate().probe(["-f", "lavfi", "-i", "rtsp://h/ok"], timeout=20)
    assert (report.codec, report.width, report.height, report.fps) == ("h264", 32, 16, 15.0)
    assert report.latency_ms >= 0


def test_probe_grabs_one_frame_through_the_same_pipe_as_capture(fake_ffmpeg):
    FFmpegTool.locate().probe(["-i", "rtsp://h/ok"], timeout=20)
    calls = fake_ffmpeg.argv()
    assert [c[0] for c in calls] == ["ffprobe", "ffmpeg"]
    grab = calls[1]
    assert grab[grab.index("-frames:v") + 1] == "1" and "rgb24" in grab


def test_a_probe_failure_is_classified_and_scrubbed(fake_ffmpeg):
    with pytest.raises(VideoSourceError) as info:
        FFmpegTool.locate().probe(
            ["-i", "rtsp://viewer:s3cret-pw@h/unauthorized"], secrets=["s3cret-pw"], timeout=20
        )
    assert info.value.code == "auth-failed"
    assert "s3cret-pw" not in str(info.value) and "viewer:" not in str(info.value)
    assert "vault" in info.value.fix


def test_probe_without_ffmpeg_is_the_install_hint(no_ffmpeg):
    with pytest.raises(FFmpegMissing):
        FFmpegTool.locate().probe(["-i", "rtsp://h/ok"])


# ---- a login never on the command line -----------------------------------------

SCRIPT = b"ffconcat version 1.0\nfile 'rtsp://viewer:pw-via-pipe@h/ok'\noption rtsp_transport tcp\n"
CONCAT = [
    "-f",
    "concat",
    "-safe",
    "0",
    "-protocol_whitelist",
    "pipe,rtsp,tcp,udp,rtp",
    "-i",
    SECRET_INPUT,
]


def test_a_secret_input_reaches_each_child_on_a_pipe_and_never_its_argv(fake_ffmpeg):
    report = FFmpegTool.locate().probe(CONCAT, secret_input=SCRIPT, timeout=20)
    assert report.codec == "h264"
    assert not any("pw-via-pipe" in " ".join(argv) for argv in fake_ffmpeg.argv())
    assert all(any(a.startswith("pipe:") for a in argv) for argv in fake_ffmpeg.argv())
    seen = fake_ffmpeg.inputs()
    assert [s["tool"] for s in seen] == ["ffprobe", "ffmpeg"], "both the probe and the grab read it"
    assert all(
        s["url"] == "rtsp://viewer:pw-via-pipe@h/ok" and s["options"] == {"rtsp_transport": "tcp"}
        for s in seen
    )


def test_a_failure_through_the_pipe_is_classified_and_scrubbed(fake_ffmpeg):
    script = SCRIPT.replace(b"/ok", b"/unauthorized")
    with pytest.raises(VideoSourceError) as info:
        FFmpegTool.locate().probe(CONCAT, secret_input=script, timeout=20)
    assert info.value.code == "auth-failed"
    assert "pw-via-pipe" not in str(info.value) and "viewer:" not in str(info.value)


def test_naming_the_secret_input_without_one_is_refused(fake_ffmpeg):
    with pytest.raises(ValueError, match="secret"):
        FFmpegTool.locate().probe(CONCAT, timeout=20)
