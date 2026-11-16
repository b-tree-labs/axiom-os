# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``Frame``: one decoded picture, which is all a trigger ever sees (spec-video §Frame).

A frame carries no source kind, so a trigger cannot tell a camera from a
recording or anything else that yields frames (ADR-240 D3). ``(stream,
session_id, seq)`` identifies a frame across an overlap switch; ``seq`` is gapless
within a session and starts again at zero when a session is re-established, with
a new ``session_id``.

numpy is the optional ``video`` extra and is imported only when a frame is built,
so importing the extension never needs it.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

#: The extra that provides numpy, named in every message about its absence.
EXTRA = "axiom-os-lm[video]"


class NumpyMissing(ImportError):
    """numpy is not installed; the message names the extra that provides it."""


def numpy_available() -> bool:
    """Is numpy importable here (without importing it into the process for nothing)?"""
    return importlib.util.find_spec("numpy") is not None


def require_numpy() -> Any:
    """The numpy module, or :class:`NumpyMissing` naming the extra."""
    try:
        import numpy
    except ImportError as exc:
        raise NumpyMissing(
            f"decoded frames are numpy arrays and numpy is not installed: pip install '{EXTRA}'"
        ) from exc
    return numpy


def utc_now_iso() -> str:
    """The wall clock now, ISO-8601 in UTC with its offset."""
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Frame:
    """One decoded frame. ``array`` is H×W×3 ``uint8`` RGB."""

    stream: str
    ts: str
    pts: float
    seq: int
    session_id: str
    array: Any

    @property
    def identity(self) -> tuple[str, str, int]:
        """``(stream, session_id, seq)``: what a subscriber drops repeats by."""
        return (self.stream, self.session_id, self.seq)

    @property
    def width(self) -> int:
        return int(self.array.shape[1])

    @property
    def height(self) -> int:
        return int(self.array.shape[0])


def frame_from_bytes(
    raw: bytes,
    *,
    width: int,
    height: int,
    stream: str,
    session_id: str,
    seq: int,
    pts: float,
    ts: str | None = None,
) -> Frame:
    """A :class:`Frame` from one raw ``rgb24`` buffer; a buffer of the wrong length is refused."""
    expected = width * height * 3
    if len(raw) != expected:
        raise ValueError(f"a {width}x{height} rgb24 frame is {expected} bytes, got {len(raw)}")
    np = require_numpy()
    array = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)
    return Frame(
        stream=stream, ts=ts or utc_now_iso(), pts=pts, seq=seq, session_id=session_id, array=array
    )


__all__ = [
    "EXTRA",
    "Frame",
    "NumpyMissing",
    "frame_from_bytes",
    "numpy_available",
    "require_numpy",
    "utc_now_iso",
]
