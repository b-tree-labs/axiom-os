# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``reference-match``: which named reference picture each region matches, or none (VID-016..021).

spec-video §Design, ``reference-match``. The trigger knows nothing about what is in
view: the regions, the reference pictures, their labels and the thresholds come from
a reference set the consumer builds (:mod:`.reference_set`), named by an absolute
path in the attachment.

Per matched frame (about one per ``sample_s`` of the frames' own time): reduce to
the set's working size by a whole factor, check the view (the landmarks against
their picture) and the exposure, and correlate each region with each of its
references. The best wins if it clears its minimum and beats the runner-up by the
region's margin; otherwise the region reads ``unknown``, with the reason. Over time,
a label is reported once it has held for ``dwell_s`` and left after ``leave_s``
without agreement; a change is one episode, and every region writes a heartbeat at
least every ``heartbeat_s``, in every state.

It is a heuristic and says so. Its scores are correlations, not confidences. numpy
is imported when the attachment is built, not at import.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .frame import Frame, require_numpy
from .luma import block_luma
from .reference_set import (
    FLAT_STD,
    RESERVED_LABELS,
    ReferenceSet,
    box_pixels,
    load_reference_set,
    visible,
)
from .triggers import BaseTrigger, TriggerConfigError, TriggerEvent, incident_key

#: Every setting but ``reference_set`` (required) and ``severity``, with its default.
DEFAULTS: dict[str, float] = {
    "sample_s": 1.0,
    "dwell_s": 15.0,
    "leave_s": 3.0,
    "heartbeat_s": 300.0,
}
#: Every key an attachment of reference-match may carry.
KEYS = frozenset({"reference_set", "severity", *DEFAULTS})
METHOD = "reference_correlation"
#: Reasons a guard gives, in the order they are reported; ``neither`` is the region's own.
GUARD_REASONS = ("view", "exposure", "flat")
#: Luma at or below / at or above these counts as clipped.
CLIPPED_LOW, CLIPPED_HIGH = 2.0, 253.0
UNKNOWN, CHANGING = RESERVED_LABELS[1], RESERVED_LABELS[0]
#: Decimals a score keeps in an event or in ``health()``.
DECIMALS = 4


def _fix(key: str) -> str:
    return f"set {key} = {DEFAULTS[key]!r} (the default), or remove it"


def _seconds(config: Mapping[str, Any], key: str, *, above_zero: bool = False) -> float:
    value = config.get(key, DEFAULTS[key])
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TriggerConfigError(f"{key} is a number of seconds, not {value!r}", fix=_fix(key))
    if value < 0 or (above_zero and value == 0):
        bound = "above 0" if above_zero else "0 or more"
        raise TriggerConfigError(f"{key} is {bound} seconds, got {value!r}", fix=_fix(key))
    return float(value)


def _path(config: Mapping[str, Any]) -> Path:
    value = config.get("reference_set")
    if not isinstance(value, str) or not value.strip():
        raise TriggerConfigError(
            "reference-match needs reference_set, the absolute path of a reference set directory",
            fix='add reference_set = "/absolute/path/to/the/set" to the attachment',
        )
    path = Path(value)
    if not path.is_absolute():
        raise TriggerConfigError(
            f"reference_set {value!r} is not an absolute path: the capture process does not run "
            "in the directory the connector file was written from",
            fix=f"give reference_set as an absolute path, such as {str(Path.cwd() / value)!r}",
        )
    return path


def _score(value: float) -> float | None:
    return None if math.isnan(value) else round(value, DECIMALS)


@dataclass(frozen=True)
class _Reading:
    """What one matched frame says about one region, before time is taken into account."""

    label: str | None
    reasons: tuple[str, ...]
    scores: Mapping[str, float]
    view: float


class _Compiled:
    """A reference set as arrays, ready to compare a working-size luma image with."""

    def __init__(self, refs: ReferenceSet) -> None:
        np = require_numpy()
        self.np = np
        width, height = refs.working_size
        self.size = (width, height)
        self.guards = refs.guards
        self.unmasked = np.ones((height, width), dtype=bool)
        for box in refs.masks:
            rows, cols = box_pixels(box, width, height)
            self.unmasked[rows, cols] = False
        lm = refs.landmarks
        self.landmarks = (
            box_pixels(lm.box, width, height),
            visible(lm.box, lm.exclude, refs.masks, width, height),
        )
        self.landmark_unit = self.unit(lm.picture.array[self.landmarks[1]])
        self.regions = []
        for region in refs.regions:
            where = box_pixels(region.box, width, height)
            seen = visible(region.box, region.exclude, refs.masks, width, height)
            units = np.stack([self.unit(r.picture.array[seen]) for r in region.references])
            self.regions.append((region, where, seen, units))

    def unit(self, values: Any) -> Any:
        """Zero mean, unit length, in float64: what a correlation is a dot product with."""
        centred = values.astype(self.np.float64) - float(values.mean(dtype=self.np.float64))
        return centred / float(self.np.linalg.norm(centred))

    def centred(self, values: Any) -> tuple[Any, float]:
        np = self.np
        x = values.astype(np.float64)
        x -= x.mean()
        norm = float(np.linalg.norm(x))
        if norm < FLAT_STD * math.sqrt(x.size):
            return None, norm
        return x, norm

    def read(self, luma: Any) -> dict[str, _Reading]:
        np = self.np
        (rows, cols), seen = self.landmarks
        x, norm = self.centred(luma[rows, cols][seen])
        view = math.nan if x is None else float(x @ self.landmark_unit) / norm
        frame = luma[self.unmasked]
        mean = float(frame.mean(dtype=np.float64))
        clipped = (
            float(np.count_nonzero((frame <= CLIPPED_LOW) | (frame >= CLIPPED_HIGH))) / frame.size
        )
        guard: list[str] = []
        if not view >= self.guards.view:  # also when the landmarks have no contrast
            guard.append("view")
        low, high = self.guards.exposure
        if not low <= mean <= high or clipped > self.guards.clipped:
            guard.append("exposure")
        out: dict[str, _Reading] = {}
        for region, (r, c), mask, units in self.regions:
            x, norm = self.centred(luma[r, c][mask])
            labels = region.labels
            reasons = list(guard)
            if x is None:
                scores = {label: math.nan for label in labels}
                reasons.append("flat")
            else:
                scores = dict(zip(labels, (float(v) for v in (units @ x) / norm), strict=True))
            label = None
            if not reasons:
                ranked = [scores[lab] for lab in labels]
                best = max(range(len(ranked)), key=lambda i: (ranked[i], -i))
                runner_up = max(v for i, v in enumerate(ranked) if i != best)
                reference = region.references[best]
                if (
                    ranked[best] >= reference.min_match
                    and ranked[best] - runner_up >= region.margin
                ):
                    label = reference.label
                else:
                    reasons.append("neither")
            out[region.name] = _Reading(label, tuple(reasons), scores, view)
        return out


class _Region:
    """What one region has read and said: its runs, its state over time and its keys."""

    def __init__(self, name: str, labels: tuple[str, ...]) -> None:
        self.name = name
        self.labels = labels
        self.status = f"{name}-status-{uuid.uuid4().hex[:8]}"
        self.reported: str | None = None
        self.left = False
        self.run: str | None = None
        self.run_t = 0.0
        self.run_ts = ""
        self.agree_t = 0.0
        self.agree_ts = ""
        self.state = UNKNOWN
        self.since: str | None = None
        self.reasons: tuple[str, ...] = ()
        self.reading: _Reading | None = None
        self.said_t: float | None = None
        self.change: str | None = None
        self.change_key: str | None = None
        self.change_from: str | None = None
        self.change_ts: str | None = None
        self.counts = {**{label: 0 for label in labels}, UNKNOWN: 0}
        self.changes = 0
        self.returned = 0

    def restart_run(self) -> None:
        """A new session, or a clock stepped back: what was seen before does not count."""
        self.run = None

    def clamp(self, t: float) -> None:
        """A clock stepped back: no clock of this region is ahead of the frames'."""
        self.agree_t = min(self.agree_t, t)
        if self.said_t is not None:
            self.said_t = min(self.said_t, t)


class ReferenceMatchTrigger(BaseTrigger):
    """A region of the view matched against a set's named reference pictures."""

    name = "reference-match"
    version = "0.1.0"
    description = (
        "Which of a set's named reference pictures each region of the view matches, or none: a "
        "heuristic that correlates each region with its references and reads unknown, with the "
        "reason, when the view, the exposure or the region cannot be trusted. The regions, "
        "pictures, labels and thresholds are the consumer's; its scores are correlations, not a "
        "confidence."
    )

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        super().__init__(config)
        unknown = sorted(set(self.config) - KEYS)
        if unknown:
            raise TriggerConfigError(
                f"reference-match has no setting {', '.join(unknown)} (its settings: "
                f"{', '.join(sorted(KEYS))})",
                fix=f"remove or correct {', '.join(unknown)}",
            )
        path = _path(self.config)
        self.sample_s = _seconds(self.config, "sample_s")
        self.dwell_s = _seconds(self.config, "dwell_s")
        self.leave_s = _seconds(self.config, "leave_s")
        self.heartbeat_s = _seconds(self.config, "heartbeat_s", above_zero=True)
        self.reference_set = load_reference_set(path)
        self._compiled = _Compiled(self.reference_set)
        self._reset()

    def _reset(self) -> None:
        self._frames = 0
        self._matched = 0
        self._last_t: float | None = None
        self._session: str | None = None
        self._factor: int | None = None
        self._regions = {r.name: _Region(r.name, r.labels) for r in self.reference_set.regions}

    def start(self) -> None:
        self._reset()

    # ---- one frame -------------------------------------------------------------------

    def _factor_of(self, frame: Frame) -> int:
        width, height = self._compiled.size
        rows, cols = int(frame.array.shape[0]), int(frame.array.shape[1])
        k = rows // height
        if k < 1 or rows != k * height or cols != k * width:
            name = self.reference_set.name
            raise TriggerConfigError(
                f"a {cols}x{rows} frame is not a whole multiple of reference set {name}'s working "
                f"size {width}x{height}, so its pictures cannot be compared with it",
                fix=f"rebuild {name} at a working size that divides {cols}x{rows} by a whole number, "
                f"or set the source to a multiple of {width}x{height} (such as {2 * width}x{2 * height})",
            )
        return k

    def observe(self, frame: Frame) -> Iterable[TriggerEvent]:
        self._frames += 1
        t = datetime.fromisoformat(frame.ts).timestamp()
        new_session = frame.session_id != self._session
        back = self._last_t is not None and t < self._last_t
        if (
            not new_session
            and not back
            and self._last_t is not None
            and t - self._last_t < self.sample_s
        ):
            return []
        if (new_session and self._session is not None) or back:
            for region in self._regions.values():
                region.restart_run()
                if back:
                    region.clamp(t)
        self._session, self._last_t = frame.session_id, t
        k = self._factor_of(frame)
        self._factor = k
        readings = self._compiled.read(block_luma(frame.array, k))
        self._matched += 1
        events: list[TriggerEvent] = []
        for name, reading in readings.items():
            events += self._step(frame, t, self._regions[name], reading)
        return events

    def _step(
        self, frame: Frame, t: float, region: _Region, reading: _Reading
    ) -> list[TriggerEvent]:
        ts = frame.ts
        observed = reading.label or UNKNOWN
        region.reading = reading
        region.counts[observed] += 1
        if region.said_t is None:
            region.said_t = t  # the heartbeat clock starts at the region's first matched frame
        if observed != region.run:
            region.run, region.run_t, region.run_ts = observed, t, ts
        if region.reported is not None and observed == region.reported:
            region.agree_t, region.agree_ts = t, ts
        before, last_seen = region.reported, region.agree_ts
        transition = first = left_now = False
        if reading.label is not None and t - region.run_t >= self.dwell_s:
            if region.reported is None:
                transition = first = True
            elif reading.label != region.reported or region.left:
                transition = True
            region.reported, region.left = reading.label, False
            region.agree_t, region.agree_ts = t, ts
        elif region.reported is not None and not region.left and t - region.agree_t >= self.leave_s:
            region.left = left_now = True
        # The state, and since when.
        if region.reported is None:
            state, reasons = UNKNOWN, reading.reasons or ("settling",)
        elif not region.left:
            state, reasons = region.reported, ()
        elif any(r in GUARD_REASONS for r in reading.reasons):
            state, reasons = UNKNOWN, reading.reasons
        else:
            state, reasons = CHANGING, reading.reasons
        if transition:
            region.since = region.run_ts
        elif state != region.state or region.since is None:
            region.since = ts
        region.state, region.reasons = state, tuple(reasons)

        events: list[TriggerEvent] = []
        if transition and first:
            events.append(
                self._event(
                    frame, region, "occurred", region.status, first=True, since=region.since
                )
            )
        elif transition:
            if region.change is None:  # reported before it was left: dwell_s < leave_s
                events.append(self._started(frame, region, before, last_seen))
            label = region.reported
            returned = region.change_from == label
            events.append(
                self._event(
                    frame,
                    region,
                    "ended",
                    region.change,
                    **{"from": region.change_from},
                    to=label,
                    since=region.since,
                    started=region.change_ts,
                    returned=returned,
                )
            )
            if returned:
                region.returned += 1
            else:
                region.changes += 1
            region.change = region.change_key = region.change_from = region.change_ts = None
        elif left_now and region.change is None:
            events.append(self._started(frame, region, region.reported, last_seen))
        elif t - region.said_t >= self.heartbeat_s:
            events.append(
                self._event(
                    frame, region, "occurred", region.status, heartbeat=True, since=region.since
                )
            )
        if events:
            region.said_t = t
        return events

    def _started(
        self, frame: Frame, region: _Region, left: str | None, last_seen: str
    ) -> TriggerEvent:
        region.change = f"{region.name}-{uuid.uuid4().hex[:8]}"
        region.change_key = incident_key(frame.stream, self.label, region.change)
        region.change_from, region.change_ts = left, frame.ts
        return self._event(
            frame, region, "started", region.change, **{"from": left}, last_seen=last_seen
        )

    def _event(
        self, frame: Frame, region: _Region, kind: str, episode: str, **extra: Any
    ) -> TriggerEvent:
        reading = region.reading
        scores = {k: _score(v) for k, v in reading.scores.items()} if reading else {}
        named = region.state if region.state in scores else None
        detail = {
            "region": region.name,
            **extra,
            "state": region.state,
            "reasons": list(region.reasons),
            "scores": scores,
            "view": _score(reading.view) if reading else None,
            "method": METHOD,
            "heuristic": True,
            "reference_set": self.reference_set.name,
            "reference_version": self.reference_set.version,
        }
        return self.event(
            frame, kind, episode, score=None if named is None else scores[named], detail=detail
        )

    # ---- what it says about itself ------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """The set, the counters, the working size, and each region's state (spec-video
        §Design, ``reference-match``, ``health()``)."""
        refs = self.reference_set
        regions = {}
        for name, region in self._regions.items():
            reading = region.reading
            regions[name] = {
                "state": region.state,
                "since": region.since,
                "label": region.reported,
                "reasons": list(region.reasons),
                "scores": {k: _score(v) for k, v in reading.scores.items()} if reading else {},
                "view": _score(reading.view) if reading else None,
                "counts": dict(region.counts),
                "changes": region.changes,
                "returned": region.returned,
                "open": region.change_key,
            }
        return {
            "reference_set": {"name": refs.name, "version": refs.version, "path": str(refs.path)},
            "heuristic": True,
            "frames": self._frames,
            "matched": self._matched,
            "analysis": None
            if self._factor is None
            else {"working_size": list(refs.working_size), "factor": self._factor},
            "regions": regions,
        }

    def stop(self) -> None:
        return None


def build(config: Mapping[str, Any]) -> ReferenceMatchTrigger:
    return ReferenceMatchTrigger(config)


__all__ = [
    "CLIPPED_HIGH",
    "CLIPPED_LOW",
    "DEFAULTS",
    "FLAT_STD",
    "GUARD_REASONS",
    "KEYS",
    "METHOD",
    "RESERVED_LABELS",
    "ReferenceMatchTrigger",
    "build",
]
