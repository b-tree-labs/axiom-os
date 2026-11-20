# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Synthetic scenes for the triggers (spec-video §Tests): a gray room with sensor noise.

Frames are built the way the capture service builds them (``Frame`` with an RGB
``uint8`` array, a session, a gapless ``seq`` and a wall-clock ``ts``), ten a
second from :data:`T0`, so a trigger's timing (``min_frames``, ``cooldown_s``) is
the scene's and not the machine's. A scene is deterministic: the noise is seeded.

The door scenes (:func:`door_scene`, for ``reference-match``) are a room with things
that never move (a window, a shelf, a picture, the floor), a clock burned into the
top-left corner that changes every frame, and one or more doors whose look follows
the scene's :class:`Segment` s. :func:`door_reference_set` writes a reference set for
them by hand, in the format spec-video §Reference set describes, from the same room
rendered without noise.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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


# ---- a room with a door, for reference-match ----------------------------------------

Box = tuple[float, float, float, float]

#: The door scenes' frame size and the working size their reference sets are taken at.
DOOR_SIZE = (320, 180)
WORKING = (160, 90)
DOOR_FPS = 2
ROOM = 110.0
#: What never moves, as (box, luma level). Boxes are fractions of the frame.
LANDMARKS: tuple[tuple[Box, float], ...] = (
    ((0.05, 0.10, 0.25, 0.40), 200.0),  # a window
    ((0.70, 0.60, 0.95, 0.70), 40.0),  # a shelf
    ((0.75, 0.15, 0.90, 0.35), 170.0),  # a picture
    ((0.78, 0.20, 0.87, 0.30), 60.0),  # what it shows
    ((0.00, 0.85, 1.00, 1.00), 70.0),  # the floor
)
#: A clock burned into the picture: it changes every frame, so a set masks it.
CLOCK: Box = (0.0, 0.0, 0.20, 0.08)
DOOR: Box = (0.40, 0.20, 0.60, 0.80)
SIDE_DOOR: Box = (0.28, 0.45, 0.36, 0.80)
#: The doors of a scene, by region name. A two-door scene passes both.
ONE_DOOR: Mapping[str, Box] = {"door": DOOR}
TWO_DOORS: Mapping[str, Box] = {"door": DOOR, "side-door": SIDE_DOOR}

#: How a door can look, as boxes inside the door (fractions of the door) and levels.
#: The first entry is the fill. ``stranger`` (someone standing in the doorway) matches
#: neither ``closed`` nor ``open``; ``flat`` (a lamp shining at the lens over the door)
#: is clipped white, so it has no contrast left.
LOOKS: Mapping[str, tuple[tuple[Box, float], ...]] = {
    "closed": (
        ((0.0, 0.0, 1.0, 1.0), 150.0),
        ((0.15, 0.08, 0.85, 0.45), 115.0),
        ((0.15, 0.55, 0.85, 0.92), 115.0),
        ((0.72, 0.46, 0.88, 0.54), 235.0),
    ),
    "open": (
        ((0.0, 0.0, 1.0, 1.0), 30.0),
        ((0.0, 0.0, 0.14, 1.0), 150.0),
        ((0.14, 0.86, 1.0, 1.0), 95.0),
    ),
    "ajar": (
        ((0.0, 0.0, 1.0, 1.0), 30.0),
        ((0.0, 0.0, 0.55, 1.0), 150.0),
        ((0.40, 0.46, 0.50, 0.54), 235.0),
    ),
    "stranger": (
        ((0.0, 0.0, 1.0, 1.0), 120.0),
        ((0.0, 0.0, 1.0, 0.12), 225.0),
        ((0.0, 0.25, 1.0, 0.37), 225.0),
        ((0.0, 0.50, 1.0, 0.62), 225.0),
        ((0.0, 0.75, 1.0, 0.87), 225.0),
    ),
    "flat": (((0.0, 0.0, 1.0, 1.0), 300.0),),
}


def _span(lo: float, hi: float, n: int) -> slice:
    """The pixels of ``[lo, hi)`` of ``n``, as spec-video §Reference set rounds a box."""
    return slice(round(lo * n), round(hi * n))


def pixels(box: Box, width: int, height: int) -> tuple[slice, slice]:
    """``(rows, cols)`` of a box in an image of ``width`` by ``height``."""
    x0, y0, x1, y1 = box
    return _span(y0, y1, height), _span(x0, x1, width)


@dataclass(frozen=True)
class Segment:
    """``seconds`` of the room with each door's ``look`` (one look, or one per door).

    ``gain`` and ``offset`` change the light on the whole room; ``ramp`` makes it
    uneven, from ``gain - ramp`` on the left to ``gain + ramp`` on the right (a lamp on
    one side); ``shift`` moves the picture by fractions of the frame (a bumped
    camera). ``session`` starts a new session here; ``gap_s`` is that many seconds
    with no frames before the segment.
    """

    seconds: float
    look: str | Mapping[str, str] = "closed"
    gain: float = 1.0
    offset: float = 0.0
    ramp: float = 0.0
    shift: tuple[float, float] = (0.0, 0.0)
    session: str | None = None
    gap_s: float = 0.0


def _paint(room: np.ndarray, box: Box, level: float) -> None:
    height, width = room.shape[:2]
    rows, cols = pixels(box, width, height)
    room[rows, cols] = level


def inside(outer: Box, inner: Box) -> Box:
    """A box given as fractions of ``outer``, as fractions of the frame."""
    x0, y0, x1, y1 = outer
    w, h = x1 - x0, y1 - y0
    return (x0 + inner[0] * w, y0 + inner[1] * h, x0 + inner[2] * w, y0 + inner[3] * h)


def render(
    size: tuple[int, int] = DOOR_SIZE,
    *,
    looks: Mapping[str, str],
    doors: Mapping[str, Box] = ONE_DOOR,
    gain: float = 1.0,
    offset: float = 0.0,
    ramp: float = 0.0,
    shift: tuple[float, float] = (0.0, 0.0),
    clock: np.ndarray | None = None,
) -> np.ndarray:
    """The room as a ``float32`` luma image, before noise: what each door looks like now."""
    width, height = size
    room = np.full((height, width), ROOM, dtype=np.float32)
    for box, level in LANDMARKS:
        _paint(room, box, level)
    for name, box in doors.items():
        for inner, level in LOOKS[looks[name]]:
            _paint(room, inside(box, inner), level)
    if clock is not None:
        rows, cols = pixels(CLOCK, width, height)
        room[rows, cols] = clock[: rows.stop - rows.start, : cols.stop - cols.start]
    light = np.linspace(gain - ramp, gain + ramp, width, dtype=np.float32)[None, :]
    room = room * light + offset
    dx, dy = round(shift[0] * width), round(shift[1] * height)
    if dx or dy:
        room = np.roll(room, (dy, dx), axis=(0, 1))
    return room


def _looks(look: str | Mapping[str, str], doors: Mapping[str, Box]) -> dict[str, str]:
    return {name: look for name in doors} if isinstance(look, str) else dict(look)


def door_scene(
    *segments: Segment,
    doors: Mapping[str, Box] = ONE_DOOR,
    size: tuple[int, int] = DOOR_SIZE,
    fps: int = DOOR_FPS,
    noise: float = 4.0,
    seed: int = 11,
    stream: str = "cam-1",
    session: str = "s1",
) -> list[Frame]:
    """The segments one after another, ``fps`` frames a second from :data:`T0`, as RGB frames."""
    width, height = size
    rng = np.random.default_rng(seed)
    out: list[Frame] = []
    t, seq = 0.0, 0
    for segment in segments:
        t += segment.gap_s
        if segment.session is not None and segment.session != session:
            session, seq = segment.session, 0
        looks = _looks(segment.look, doors)
        for _ in range(round(segment.seconds * fps)):
            rows, cols = pixels(CLOCK, width, height)
            clock = rng.integers(0, 256, size=(rows.stop - rows.start, cols.stop - cols.start))
            luma = render(
                size,
                looks=looks,
                doors=doors,
                gain=segment.gain,
                offset=segment.offset,
                ramp=segment.ramp,
                shift=segment.shift,
                clock=clock.astype(np.float32),
            )
            if noise:
                luma = luma + rng.normal(0.0, noise, size=luma.shape).astype(np.float32)
            gray = np.clip(np.rint(luma), 0, 255).astype(np.uint8)
            out.append(
                Frame(
                    stream=stream,
                    ts=(T0 + timedelta(seconds=t)).isoformat(),
                    pts=t,
                    seq=seq,
                    session_id=session,
                    array=np.repeat(gray[:, :, None], 3, axis=2),
                )
            )
            t += 1.0 / fps
            seq += 1
    return out


def seconds_after_t0(ts: str) -> float:
    return (datetime.fromisoformat(ts) - T0).total_seconds()


def _working(image: np.ndarray, working: tuple[int, int]) -> np.ndarray:
    """A block mean down to the working size: the reduction a reference is taken through."""
    height, width = image.shape
    w, h = working
    k = height // h
    assert (width, height) == (w * k, h * k), "the scene's size is a whole multiple of WORKING"
    return image.reshape(h, k, w, k).mean(axis=(1, 3))


def _npy(path: Path, image: np.ndarray) -> str:
    np.save(path, np.asarray(image, dtype=np.float32), allow_pickle=False)
    return hashlib.sha256(path.read_bytes()).hexdigest()


#: The guards a door set declares unless a test says otherwise.
GUARDS: Mapping[str, Any] = {"view": 0.9, "exposure": [40.0, 220.0], "clipped": 0.05}


@dataclass(frozen=True)
class DoorSet:
    """What :func:`door_reference_set` wrote: the directory and the manifest it holds."""

    path: Path
    manifest: dict[str, Any] = field(default_factory=dict)

    @property
    def manifest_path(self) -> Path:
        return self.path / "reference-set.json"

    def rewrite(self, **changes: Any) -> DoorSet:
        """The manifest with top-level ``changes``, written back as is (no hash is refreshed)."""
        manifest = {**self.manifest, **changes}
        self.manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return DoorSet(self.path, manifest)


def door_reference_set(
    directory: Path,
    *,
    regions: Mapping[str, Mapping[str, str]] | None = None,
    doors: Mapping[str, Box] = ONE_DOOR,
    size: tuple[int, int] = DOOR_SIZE,
    working: tuple[int, int] = WORKING,
    masks: Sequence[Box] = (CLOCK,),
    margin: float = 0.3,
    min_match: float = 0.6,
    guards: Mapping[str, Any] = GUARDS,
    name: str = "test-room",
    version: str = "1",
) -> DoorSet:
    """Write a reference set for the door scenes, by hand, and return where.

    ``regions`` is ``{region: {label: look}}``; the default is one region ``door``
    with ``closed`` and ``open``. Every picture is the noiseless room reduced to the
    working size, so a scene's frames match the look they show.
    """
    regions = regions or {"door": {"closed": "closed", "open": "open"}}
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    w, h = working
    every = {d: next(iter(regions.get(d, {"": "closed"}).values())) for d in doors}
    base = _working(render(size, looks=every, doors=doors), working)
    rows, cols = pixels((0.0, 0.0, 1.0, 1.0), w, h)
    landmarks = {
        "bounds": [0.0, 0.0, 1.0, 1.0],
        "exclude": [list(doors[r]) for r in regions],
        "image": "landmarks.npy",
        "sha256": _npy(directory / "landmarks.npy", base[rows, cols]),
    }
    declared = []
    for region, labels in regions.items():
        box = doors[region]
        r, c = pixels(box, w, h)
        references = []
        for label, look in labels.items():
            picture = _working(render(size, looks={**every, region: look}, doors=doors), working)
            file = f"{region}-{label}.npy"
            references.append(
                {
                    "label": label,
                    "image": file,
                    "sha256": _npy(directory / file, picture[r, c]),
                    "min_match": min_match,
                }
            )
        declared.append(
            {
                "name": region,
                "bounds": list(box),
                "exclude": [],
                "margin": margin,
                "references": references,
            }
        )
    manifest = {
        "format": "reference-set/1",
        "name": name,
        "version": version,
        "working_size": [w, h],
        "masks": [list(m) for m in masks],
        "landmarks": landmarks,
        "regions": declared,
        "guards": dict(guards),
        "provenance": {"built_by": "video/tests/scenes.py"},
    }
    out = DoorSet(directory, manifest)
    out.manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return out


__all__ = [
    "CLOCK",
    "DOOR",
    "DOOR_FPS",
    "DOOR_SIZE",
    "DoorSet",
    "FPS",
    "GUARDS",
    "LOOKS",
    "ONE_DOOR",
    "SIDE_DOOR",
    "SIZE",
    "Segment",
    "Square",
    "T0",
    "TWO_DOORS",
    "WORKING",
    "crossing",
    "door_reference_set",
    "door_scene",
    "frames",
    "inside",
    "pixels",
    "render",
    "seconds_after_t0",
    "ts_at",
]
