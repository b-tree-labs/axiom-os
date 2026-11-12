# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The preconditions the video skills declare (ADR-185), each with its exact fix.

A check is cheap and side-effect free; ``invoke_capability`` evaluates it before
the skill runs and refuses with the fix when it is not met.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skill_declarations import Precondition

from ._common import source_name, state_dir


def source_declared(params: dict[str, Any], ctx: Any) -> tuple[bool, str]:
    from ..connector import ConnectorNotFound, NotAVideoConnector, load
    from ..sources import InvalidSourceConfig

    name = source_name(params)
    if not name:
        return False, "no source was named"
    try:
        load(name, state_dir=state_dir(ctx))
    except (ConnectorNotFound, NotAVideoConnector, InvalidSourceConfig) as exc:
        return False, str(exc)
    return True, ""


def source_verified(params: dict[str, Any], ctx: Any) -> tuple[bool, str]:
    from ..state import read_verify_record

    name = source_name(params)
    record = read_verify_record(name, state_dir(ctx)) if name else None
    if record is None:
        return False, f"{name or 'the source'} has never been verified, so it is not active"
    if not record.ok:
        return False, f"{name}'s last verify failed ({record.error}), so it is not active"
    return True, ""


def ffmpeg_available(params: dict[str, Any], ctx: Any) -> tuple[bool, str]:
    from ..ffmpeg import FFmpegTool

    tool = FFmpegTool.locate()
    if tool.available:
        return True, ""
    missing = " and ".join(
        n for n, p in (("ffmpeg", tool.ffmpeg), ("ffprobe", tool.ffprobe)) if not p
    )
    return False, f"{missing} not found on this machine"


def numpy_available(params: dict[str, Any], ctx: Any) -> tuple[bool, str]:
    from ..frame import numpy_available as available

    return (True, "") if available() else (False, "numpy is not installed")


SOURCE_DECLARED = Precondition(
    "source-declared",
    "the source must be declared (a connector of kind video)",
    check=source_declared,
    fix="declare it with `{cli} instrument video add {name} --kind rtsp --url <rtsp://host/path> --credential-ref <ref>`; "
    "`{cli} instrument video list` shows what is declared",
)

SOURCE_VERIFIED = Precondition(
    "source-verified",
    "the source must have produced a frame: it is not active until its last verify passed",
    check=source_verified,
    fix="run `{cli} instrument video verify {name}`; if it fails, `{cli} instrument video diagnose {name}` names the rung and its fix",
)

FFMPEG_AVAILABLE = Precondition(
    "ffmpeg-available",
    "ffmpeg and ffprobe must be installed on this machine: decoding is an ffmpeg subprocess",
    check=ffmpeg_available,
    fix="install ffmpeg (`sudo apt install ffmpeg`, `brew install ffmpeg` or `winget install ffmpeg`), "
    "or set AXIOM_VIDEO_FFMPEG and AXIOM_VIDEO_FFPROBE to where it is",
)

NUMPY_AVAILABLE = Precondition(
    "numpy-available",
    "numpy must be installed: decoded frames are numpy arrays",
    check=numpy_available,
    fix="pip install 'axiom-os-lm[video]'",
)

__all__ = [
    "FFMPEG_AVAILABLE",
    "NUMPY_AVAILABLE",
    "SOURCE_DECLARED",
    "SOURCE_VERIFIED",
    "ffmpeg_available",
    "numpy_available",
    "source_declared",
    "source_verified",
]
