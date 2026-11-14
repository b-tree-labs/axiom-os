# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""What ``doctor`` asks of the video extension: can this node decode what it captures?

ffmpeg is a system dependency of a node that captures, not of every node (ADR-240
Consequences), so the check is skipped where no video source is declared.
"""

from __future__ import annotations

from typing import Any


def _sources(n: int) -> str:
    return f"{n} video source{'' if n == 1 else 's'}"


def check_video_capture() -> Any:
    """A ``doctor`` ``CheckResult``: skipped, ok (naming the ffmpeg found), or an error with the fix."""
    from axiom.cli.doctor import CheckResult, CheckStatus

    from .connector import list_connectors
    from .ffmpeg import FFmpegTool, install_hint
    from .frame import EXTRA, numpy_available

    names = [c.name for c in list_connectors()]
    if not names:
        return CheckResult(
            status=CheckStatus.SKIPPED, summary="no video source is declared on this node"
        )
    tool = FFmpegTool.locate()
    if not tool.available:
        missing = " and ".join(
            n for n, p in (("ffmpeg", tool.ffmpeg), ("ffprobe", tool.ffprobe)) if not p
        )
        return CheckResult(
            status=CheckStatus.ERROR,
            summary=f"{_sources(len(names))} declared, and {missing} is not installed, so none can be captured",
            fix_hint=install_hint(),
            detail={"sources": names},
        )
    if not numpy_available():
        return CheckResult(
            status=CheckStatus.ERROR,
            summary=f"{_sources(len(names))} declared, and numpy is not installed, so frames cannot be built",
            fix_hint=f"pip install '{EXTRA}'",
            detail={"sources": names, "ffmpeg": tool.ffmpeg},
        )
    return CheckResult(
        status=CheckStatus.OK,
        summary=f"{tool.version()} decodes for {_sources(len(names))}",
        detail={"sources": names, "ffmpeg": tool.ffmpeg, "ffprobe": tool.ffprobe},
    )


__all__ = ["check_video_capture"]
