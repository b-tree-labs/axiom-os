# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Synthetic scenes for the triggers (spec-video §Tests): a gray room with sensor noise.

Frames are built the way the capture service builds them (``Frame`` with an RGB
``uint8`` array, a session, a gapless ``seq`` and a wall-clock ``ts``), ten a
second from :data:`T0`, so a trigger's timing (``min_frames``, ``cooldown_s``) is
the scene's and not the machine's. A scene is deterministic: the noise is seeded.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np

from axiom.extensions.builtins.video.frame import Frame

T0 = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
FPS = 10
SIZE = (320, 180)


def ts_at(i: int, *, fps: int = FPS) -> str:
    return (T0 + timedelta(seconds=i / fps)).isoformat()


@dataclass(frozen=True)
class Square:
    """A bright square crossing the room during frames ``[first, last)``."""

    first: int
    last: int
    side: int = 50
    speed: int = 6
    x0: int = 0
    y: int = 60
    value: int = 255

    def at(self, i: int, width: int) -> int | None:
        if not self.first <= i < self.last:
            return None
        return (self.x0 + (i - self.first) * self.speed) % max(1, width - self.side)


def frames(
    n: int,
    *,
    size: tuple[int, int] = SIZE,
    background: int = 100,
    noise: float = 6.0,
    squares: tuple[Square, ...] = (),
    light_step: tuple[int, int] | None = None,
    seed: int = 7,
    stream: str = "cam-1",
    session: str = "s1",
    start: int = 0,
    fps: int = FPS,
) -> Iterator[Frame]:
    """``n`` frames of a gray room. ``light_step=(at, delta)`` brightens the whole room from ``at``."""
    width, height = size
    rng = np.random.default_rng(seed)
    for k in range(n):
        i = start + k
        level = background + (light_step[1] if light_step and i >= light_step[0] else 0)
        room = np.full((height, width, 3), float(level), dtype=np.float32)
        for square in squares:
            x = square.at(i, width)
            if x is not None:
                room[square.y : square.y + square.side, x : x + square.side, :] = square.value
        if noise:
            room += rng.normal(0.0, noise, size=room.shape).astype(np.float32)
        array = np.clip(room, 0, 255).astype(np.uint8)
        yield Frame(
            stream=stream, ts=ts_at(i, fps=fps), pts=k / fps, seq=k, session_id=session, array=array
        )


def crossing(n: int = 180, **kw) -> list[Frame]:
    """A square crosses the room for three seconds (frames 20 to 50), then ten quiet seconds."""
    return list(frames(n, squares=(Square(20, 50),), **kw))


__all__ = ["FPS", "SIZE", "Square", "T0", "crossing", "frames", "ts_at"]
