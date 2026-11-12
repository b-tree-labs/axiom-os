# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A ``Frame`` is what a trigger sees, and it cannot tell a camera from anything else.

spec-video §Frame: ``stream``, ``ts`` (ISO-8601 UTC at decode), ``pts``, ``seq``
(gapless per session), ``session_id``, ``array`` (H×W×3 uint8 RGB). ``(stream,
session_id, seq)`` identifies a frame across an overlap switch. ADR-240 D3.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime

import pytest

from axiom.extensions.builtins.video.frame import Frame, frame_from_bytes

from .fakes import frame_bytes


def test_the_fields_are_exactly_the_contract():
    assert [f.name for f in dataclasses.fields(Frame)] == [
        "stream",
        "ts",
        "pts",
        "seq",
        "session_id",
        "array",
    ]


def test_a_frame_carries_no_source_kind():
    names = {f.name for f in dataclasses.fields(Frame)}
    assert not names & {"kind", "source", "source_kind", "url"}


def test_raw_rgb24_becomes_an_h_by_w_by_3_uint8_array():
    import numpy as np

    raw = bytes(range(4 * 2 * 3))
    frame = frame_from_bytes(raw, width=4, height=2, stream="cam", session_id="s", seq=0, pts=0.0)
    assert frame.array.shape == (2, 4, 3)
    assert frame.array.dtype == np.uint8
    assert frame.array[0, 0].tolist() == [0, 1, 2], "rgb triplets in row-major order"
    assert frame.array[1, 3].tolist() == [21, 22, 23]
    assert (frame.width, frame.height) == (4, 2)


def test_the_wall_clock_is_iso_8601_utc():
    frame = frame_from_bytes(
        frame_bytes(2, 2), width=2, height=2, stream="cam", session_id="s", seq=0, pts=0.0
    )
    parsed = datetime.fromisoformat(frame.ts)
    assert parsed.utcoffset() is not None and parsed.utcoffset().total_seconds() == 0


def test_a_short_buffer_is_refused_not_reshaped():
    with pytest.raises(ValueError, match="12"):
        frame_from_bytes(
            b"\x00" * 11, width=2, height=2, stream="cam", session_id="s", seq=0, pts=0.0
        )


def test_identity_is_stream_session_and_seq():
    frame = frame_from_bytes(
        frame_bytes(2, 2), width=2, height=2, stream="cam", session_id="s9", seq=41, pts=4.1
    )
    assert frame.identity == ("cam", "s9", 41)


def test_a_frame_is_immutable():
    frame = frame_from_bytes(
        frame_bytes(2, 2), width=2, height=2, stream="cam", session_id="s", seq=0, pts=0.0
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        frame.seq = 1  # type: ignore[misc]
