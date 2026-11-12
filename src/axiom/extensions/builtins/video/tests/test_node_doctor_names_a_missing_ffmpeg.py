# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``doctor`` (and ``node doctor``) report a capture node that cannot decode (ADR-240 Consequences).

ffmpeg is a system dependency of a node that captures, not of every node: the
check is skipped where no video source is declared, so an install with no camera
is not told to install a video decoder. Where sources are declared and ffmpeg is
missing, it is an error naming the install line for this platform.
"""

from __future__ import annotations

from axiom.cli.doctor import CheckStatus, default_checks
from axiom.extensions.builtins.video import connector as vc
from axiom.extensions.builtins.video.health import check_video_capture


def _declare(state_dir):
    vc.connect(
        vc.VideoConnector(name="cam-1", source={"kind": "rtsp", "url": "rtsp://h/ok"}),
        state_dir=state_dir,
    )


def test_a_node_with_no_video_source_skips_it_even_without_ffmpeg(state_dir, no_ffmpeg):
    result = check_video_capture()
    assert result.status is CheckStatus.SKIPPED
    assert "no video source" in result.summary


def test_a_capture_node_without_ffmpeg_is_an_error_with_the_install_line(state_dir, no_ffmpeg):
    _declare(state_dir)
    result = check_video_capture()
    assert result.status is CheckStatus.ERROR
    assert "ffmpeg" in result.summary and "1 video source" in result.summary
    assert "install ffmpeg" in result.fix_hint
    assert result.detail["sources"] == ["cam-1"]


def test_a_capture_node_with_ffmpeg_passes_and_says_which(state_dir, fake_ffmpeg):
    _declare(state_dir)
    result = check_video_capture()
    assert result.status is CheckStatus.OK
    assert "fake" in result.summary, "it names the ffmpeg it found"
    assert result.detail["ffmpeg"] == str(fake_ffmpeg.ffmpeg)


def test_it_is_one_of_the_doctors_checks():
    assert any(c.fn is check_video_capture for c in default_checks())


def test_node_doctor_reports_it_with_its_fix(state_dir, no_ffmpeg):
    from axiom.extensions.builtins.node.skills.doctor import gather

    _declare(state_dir)
    (finding,) = gather("ffmpeg")["findings"]
    assert finding["severity"] == "error" and "install ffmpeg" in finding["fix"]
