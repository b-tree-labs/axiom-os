# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``motion``: the first trigger, frame differencing over a region (VID-014, spec-video §motion).

Each frame: crop to the region, grayscale, downscale so the longer side is at most
:data:`MAX_SIDE` px (a block mean, which also averages sensor noise down), take the
absolute difference against a running-average background (:data:`ALPHA`), count
the pixels that changed by more than :data:`THRESHOLD`/255, and
``score = changed / total``. ``score ≥ min_area`` for ``min_frames`` frames in a row
opens an episode (``started``, a new incident key); ``score < min_area`` for
``cooldown_s`` closes it (``ended``).

A new session (a reconnect) resets the background, and the first
:data:`LEARN_FRAMES` frames of a session learn it as a plain mean without being
scored: a background that starts as one noisy frame carries that frame's noise
into every difference, and a dim camera would open an episode after every
reconnect. An episode open across a reconnect stays open and closes after
``cooldown_s`` of quiet on the new session.

It is a heuristic and says so: any change of light counts, not only things that
move. numpy is imported when the first frame is observed, not at import.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from .frame import Frame, require_numpy
from .triggers import BaseTrigger, TriggerConfigError, TriggerEvent

ALPHA = 0.05
THRESHOLD = 25
MAX_SIDE = 320
#: Frames a new session spends learning its background: 1/α.
LEARN_FRAMES = round(1 / ALPHA)
DEFAULTS: dict[str, Any] = {"roi": "full", "min_area": 0.02, "min_frames": 3, "cooldown_s": 5.0}
#: Every key an attachment of motion may carry.
KEYS = frozenset({"severity", *DEFAULTS})
METHOD = "frame_diff"
#: ITU-R BT.601 luma, the grayscale every camera's "Y" is.
_LUMA = (0.299, 0.587, 0.114)


def _fix(key: str) -> str:
    return f"set {key} = {DEFAULTS[key]!r} (the default), or remove it"


def parse_roi(value: Any) -> tuple[int, int, int, int] | None:
    """``"x,y,w,h"`` in source pixels, or ``None`` for ``"full"``; :class:`TriggerConfigError` otherwise."""
    if value in (None, "", "full"):
        return None
    try:
        x, y, w, h = (int(part.strip()) for part in str(value).split(","))
    except ValueError:
        raise TriggerConfigError(
            f'roi {value!r} is not "x,y,w,h" in pixels, or "full"',
            fix='set roi = "full", or a region such as roi = "960,540,640,360"',
        ) from None
    if x < 0 or y < 0 or w < 1 or h < 1:
        raise TriggerConfigError(
            f"roi {value!r} must start at or after 0,0 and be at least 1x1",
            fix='set roi = "full", or a region such as roi = "960,540,640,360"',
        )
    return x, y, w, h


def _number(config: Mapping[str, Any], key: str, *, whole: bool = False) -> Any:
    value = config.get(key, DEFAULTS[key])
    kinds = (int,) if whole else (int, float)
    if isinstance(value, bool) or not isinstance(value, kinds):
        raise TriggerConfigError(
            f"{key} is a {'whole number' if whole else 'number'}, not {value!r}", fix=_fix(key)
        )
    return value


class MotionTrigger(BaseTrigger):
    """Frame differencing against a running-average background, with per-source thresholds."""

    name = "motion"
    version = "0.1.0"
    description = (
        "Frame differencing over a region: a heuristic that fires on any change in view, a change "
        "of light or a shadow as much as something moving; it does not detect objects or people."
    )

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        super().__init__(config)
        unknown = sorted(set(self.config) - KEYS)
        if unknown:
            raise TriggerConfigError(
                f"motion has no setting {', '.join(unknown)} (its settings: {', '.join(sorted(KEYS))})",
                fix=f"remove or correct {', '.join(unknown)}",
            )
        self.roi_text = str(self.config.get("roi", DEFAULTS["roi"]) or "full")
        self.roi = parse_roi(self.roi_text)
        self.min_area = float(_number(self.config, "min_area"))
        if not 0.0 < self.min_area <= 1.0:
            raise TriggerConfigError(
                f"min_area is the fraction of the region that must change, above 0 and at most 1; "
                f"got {self.min_area!r}",
                fix=_fix("min_area"),
            )
        self.min_frames = int(_number(self.config, "min_frames", whole=True))
        if self.min_frames < 1:
            raise TriggerConfigError(
                f"min_frames is at least 1, got {self.min_frames}", fix=_fix("min_frames")
            )
        self.cooldown_s = float(_number(self.config, "cooldown_s"))
        if self.cooldown_s < 0:
            raise TriggerConfigError(
                f"cooldown_s is 0 or more seconds, got {self.cooldown_s!r}", fix=_fix("cooldown_s")
            )
        self._reset_counters()

    def _reset_counters(self) -> None:
        self._frames = 0
        self._episodes = 0
        self._last_score: float | None = None
        self._analysis: list[int] | None = None
        self._session: str | None = None
        self._background: Any = None
        self._learned = 0
        self._above = 0
        self._quiet_since: float | None = None
        self._episode: str | None = None
        self._open_key: str | None = None
        self._episode_started: str | None = None
        self._peak = 0.0
        self._np: Any = None
        self._luma: Any = None

    def start(self) -> None:
        self._np = require_numpy()
        self._luma = self._np.asarray(_LUMA, dtype=self._np.float32)

    # ---- one frame -------------------------------------------------------------

    def _region(self, array: Any) -> Any:
        if self.roi is None:
            return array
        x, y, w, h = self.roi
        height, width = int(array.shape[0]), int(array.shape[1])
        if x + w > width or y + h > height:
            raise TriggerConfigError(
                f"roi {self.roi_text} reaches outside the {width}x{height} frame",
                fix=f'set roi = "full", or a region inside {width}x{height}',
            )
        return array[y : y + h, x : x + w]

    def _gray(self, region: Any) -> Any:
        """Grayscale, block-averaged so the longer side is at most MAX_SIDE px.

        Summed in two steps with integer accumulators: a 4K frame is about 12 ms,
        against about 120 ms for a float mean over the whole frame.
        """
        np = self._np
        height, width = int(region.shape[0]), int(region.shape[1])
        k = max(1, math.ceil(max(height, width) / MAX_SIDE))
        if k == 1:
            return region.astype(np.float32) @ self._luma
        rows, cols = height // k, width // k
        block = region[: rows * k, : cols * k]
        narrow = np.uint16 if k * 255 <= np.iinfo(np.uint16).max else np.uint32
        summed = block.reshape(rows, k, cols * k, 3).sum(axis=1, dtype=narrow)
        summed = summed.reshape(rows, cols, k, 3).sum(axis=2, dtype=np.uint32)
        return (summed.astype(np.float32) @ self._luma) / float(k * k)

    def observe(self, frame: Frame) -> Iterable[TriggerEvent]:
        if self._np is None:
            self.start()
        self._frames += 1
        if frame.session_id != self._session:
            # A new session: the camera may have moved, re-exposed or rebooted. Learn again.
            self._session, self._background, self._learned = frame.session_id, None, 0
            self._above, self._quiet_since = 0, None
        gray = self._gray(self._region(frame.array))
        self._analysis = [int(gray.shape[1]), int(gray.shape[0])]
        if self._learned < LEARN_FRAMES:
            if self._background is None:
                self._background = gray.copy()
            else:
                self._background += (gray - self._background) / (self._learned + 1)
            self._learned += 1
            return []
        np = self._np
        score = float(np.count_nonzero(np.abs(gray - self._background) > THRESHOLD)) / gray.size
        self._background += ALPHA * (gray - self._background)
        self._last_score = score
        return self._step(frame, score)

    def _step(self, frame: Frame, score: float) -> list[TriggerEvent]:
        at = datetime.fromisoformat(frame.ts).timestamp()
        if score >= self.min_area:
            self._above += 1
            self._quiet_since = None
            if self._episode is not None:
                self._peak = max(self._peak, score)
                return []
            if self._above < self.min_frames:
                return []
            self._episode = uuid.uuid4().hex[:8]
            self._episode_started = frame.ts
            self._episodes += 1
            self._peak = score
            started = self.event(
                frame, "started", self._episode, score=score, detail=self._detail()
            )
            self._open_key = started.incident_key
            return [started]
        self._above = 0
        if self._episode is None:
            return []
        if self._quiet_since is None:
            self._quiet_since = at
        if at - self._quiet_since < self.cooldown_s:
            return []
        started = self._episode_started or frame.ts
        detail = {
            **self._detail(),
            "score_peak": round(self._peak, 6),
            "started": started,
            "duration_s": round(at - datetime.fromisoformat(started).timestamp(), 3),
        }
        ended = self.event(frame, "ended", self._episode, score=score, detail=detail)
        self._episode = self._episode_started = self._open_key = None
        self._peak = 0.0
        return [ended]

    def _detail(self) -> dict[str, Any]:
        return {
            "method": METHOD,
            "heuristic": True,
            "roi": self.roi_text,
            "min_area": self.min_area,
        }

    def health(self) -> dict[str, Any]:
        """``frames``, ``episodes``, ``open`` (its incident key), ``last_score``, ``learning``, ``analysis``."""
        return {
            "frames": self._frames,
            "episodes": self._episodes,
            "open": self._open_key,
            "last_score": None if self._last_score is None else round(self._last_score, 6),
            "learning": self._session is not None and self._learned < LEARN_FRAMES,
            "analysis": self._analysis,
        }

    def stop(self) -> None:
        self._background = None


def build(config: Mapping[str, Any]) -> MotionTrigger:
    return MotionTrigger(config)


__all__ = [
    "ALPHA",
    "DEFAULTS",
    "KEYS",
    "LEARN_FRAMES",
    "MAX_SIDE",
    "METHOD",
    "MotionTrigger",
    "THRESHOLD",
    "build",
    "parse_roi",
]
