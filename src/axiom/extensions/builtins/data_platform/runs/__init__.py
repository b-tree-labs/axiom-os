# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A run: a bounded period of operation, with what it was configured to do.

Signals are continuous; the questions people ask are not. "How did this loop
behave at 500 W" and "is this startup like the last one" are questions about
*runs*, and until there is a run entity every such question is answered by a
hand-picked time range that nobody can reproduce and no two people pick the
same way.

Three things this carries that a time range cannot.

**Configuration, in normalised keys.** TAMU declares power setpoints and a
volumetric flow rate per run; VCU and ACU will declare their own. Config keys
are to runs exactly what roles are to channels: the site keeps its own words,
and a shared key is what makes two runs comparable without either site being
renamed. A run that declares no configuration is *not* matchable, and
:func:`similar_runs` says so rather than guessing — two runs are not alike
because nothing is known about either.

**Segments, with the label's provenance attached.** A steady-state window that
a person marked and one that an algorithm detected are different kinds of
claim, and the difference has to survive into the data. So every segment
carries ``label_source`` — ``curated`` with the curator named, or ``inferred``
with a confidence — and they are never merged into one column. Curated labels
are ground truth; an inferred label is a hypothesis that gets *scored against*
them, and a schema that cannot tell them apart makes that scoring impossible.

**Whether the run happened at all.** A simulated run is not a short real one.
It carries the same ``source_class`` vocabulary as a signal row, for the same
reason: a comparison that quietly includes synthetic runs is worse than one
that finds no peers.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

#: What a segment is. Deliberately about *behaviour*, not about cause: a
#: transient is a transient whether a rod moved or a pump tripped, and the
#: cause belongs in a note rather than in a label that charts group by.
SEGMENT_LABELS: tuple[str, ...] = (
    "steady_state",
    "transient",
    "startup",
    "shutdown",
    "pulse",
    "fault",
    "unknown",
)

#: Where a segment's label came from. The whole point of the column.
#:
#: ``curated``  — a person marked this window. Ground truth.
#: ``recorded`` — the plant itself declared it. A reactor console's mode
#:                annunciator is not somebody's opinion and not a detector's
#:                guess; it is the machine stating what it was doing. Ground
#:                truth of a different kind, and it needs neither a curator
#:                (no person typed it) nor a confidence (nothing estimated it).
#: ``inferred`` — an algorithm detected it. A hypothesis, scored against the
#:                other two.
#:
#: `recorded` exists because collapsing it into either neighbour loses
#: something. Calling it `curated` would invent a human who never acted;
#: calling it `inferred` would rank a plant's own statement alongside a
#: detector's guess and let a detector "disagree" with the machine it is
#: modelling.
LABEL_SOURCES: tuple[str, ...] = ("curated", "recorded", "inferred")

#: Label sources that are ground truth — what an inferred label is scored
#: against, and what a disagreement should be resolved in favour of.
GROUND_TRUTH_SOURCES: frozenset[str] = frozenset({"curated", "recorded"})

#: How a run's configuration was obtained.
CONFIG_SOURCES: tuple[str, ...] = ("declared", "derived")

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

RUNS_DDL = [
    "CREATE SCHEMA IF NOT EXISTS silver",
    """CREATE TABLE IF NOT EXISTS silver.runs (
         site          text NOT NULL,
         run_id        text NOT NULL,
         started_at    timestamptz NOT NULL,
         -- NULL means still running. Not a sentinel far-future date: a range
         -- that claims to end in 2999 sorts and filters as though it had an
         -- end, and every query then has to know the sentinel.
         ended_at      timestamptz,
         config        jsonb NOT NULL DEFAULT '{}'::jsonb,
         config_source text NOT NULL DEFAULT 'declared',
         source_class  text NOT NULL DEFAULT 'measured',
         model_ref     text,
         notes         text,
         PRIMARY KEY (site, run_id)
       )""",
    """CREATE INDEX IF NOT EXISTS runs_site_started
       ON silver.runs (site, started_at DESC)""",
    """CREATE TABLE IF NOT EXISTS silver.run_segments (
         site         text NOT NULL,
         run_id       text NOT NULL,
         segment_id   text NOT NULL,
         label        text NOT NULL,
         started_at   timestamptz NOT NULL,
         ended_at     timestamptz,
         -- 'curated' or 'inferred'. Never merged with `label`: a window a
         -- person marked and one an algorithm detected are different claims,
         -- and an inferred label is scored AGAINST the curated ones.
         label_source text NOT NULL,
         -- Who marked it. Required for a curated label — an unattributable
         -- human judgement cannot be questioned or corrected.
         labelled_by  text,
         -- Only meaningful for an inferred label. NULL for curated: a person
         -- did not assign themselves a probability.
         confidence   double precision,
         notes        text,
         PRIMARY KEY (site, run_id, segment_id)
       )""",
    """CREATE INDEX IF NOT EXISTS run_segments_label
       ON silver.run_segments (site, label, started_at)""",
    "COMMENT ON TABLE silver.runs IS "
    "'A bounded period of operation and what it was configured to do'",
    "COMMENT ON TABLE silver.run_segments IS "
    "'Labelled windows within a run; label_source separates curated from inferred'",
]


class RunError(ValueError):
    """A run or segment that could not be admitted, and why."""


@dataclass(frozen=True)
class RunSegment:
    """A labelled window inside a run."""

    segment_id: str
    label: str
    started_at: datetime
    label_source: str
    ended_at: datetime | None = None
    labelled_by: str | None = None
    confidence: float | None = None
    notes: str | None = None

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not _ID.match(self.segment_id or ""):
            errors.append(f"segment_id {self.segment_id!r} is not a usable id")
        if self.label not in SEGMENT_LABELS:
            errors.append(f"label {self.label!r} is not one of {', '.join(SEGMENT_LABELS)}")
        if self.label_source not in LABEL_SOURCES:
            errors.append(
                f"label_source {self.label_source!r} is not one of {', '.join(LABEL_SOURCES)}"
            )
        if self.label_source == "curated" and not (self.labelled_by or "").strip():
            errors.append(
                "a curated segment must name who labelled it — an unattributable "
                "human judgement cannot be questioned or corrected"
            )
        if self.label_source == "recorded" and (self.labelled_by or "").strip():
            errors.append(
                "a recorded segment names no curator — the plant declared it, "
                "and attributing it to a person invents an author"
            )
        if self.label_source == "recorded" and self.confidence is not None:
            errors.append(
                "a recorded segment carries no confidence — the machine stated "
                "its mode, it did not estimate it"
            )
        if self.label_source == "curated" and self.confidence is not None:
            errors.append(
                "a curated segment carries no confidence — a person did not assign "
                "themselves a probability, and a number here would be invented"
            )
        if self.label_source == "inferred" and self.confidence is None:
            errors.append(
                "an inferred segment must state its confidence — otherwise it reads "
                "with the same authority as a person's judgement"
            )
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            errors.append(f"confidence {self.confidence} is outside 0..1")
        if self.ended_at is not None and self.ended_at < self.started_at:
            errors.append("ended_at is before started_at")
        return errors


@dataclass(frozen=True)
class Run:
    """A bounded period of operation at one site."""

    site: str
    run_id: str
    started_at: datetime
    ended_at: datetime | None = None
    #: Normalised configuration keys — ``power_setpoint_w``, ``flow_rate_lpm``.
    #: Shared keys are what make two runs comparable; a site's own spelling
    #: stays in its ingest, exactly as a channel keeps its name.
    config: dict[str, Any] = field(default_factory=dict)
    config_source: str = "declared"
    source_class: str = "measured"
    model_ref: str | None = None
    segments: tuple[RunSegment, ...] = ()
    notes: str | None = None

    @property
    def open(self) -> bool:
        return self.ended_at is None

    @property
    def ground_truth_segments(self) -> tuple[RunSegment, ...]:
        """Curated and recorded together — what a detector is scored against."""
        return tuple(s for s in self.segments if s.label_source in GROUND_TRUTH_SOURCES)

    @property
    def curated_segments(self) -> tuple[RunSegment, ...]:
        return tuple(s for s in self.segments if s.label_source == "curated")

    @property
    def inferred_segments(self) -> tuple[RunSegment, ...]:
        return tuple(s for s in self.segments if s.label_source == "inferred")

    def validate(self) -> list[str]:
        from axiom.extensions.builtins.data_platform.daq.envelope import (
            MODELLED_CLASSES,
            SOURCE_CLASSES,
        )

        errors: list[str] = []
        if not (self.site or "").strip():
            errors.append("site is empty")
        if not _ID.match(self.run_id or ""):
            errors.append(f"run_id {self.run_id!r} is not a usable id")
        if self.config_source not in CONFIG_SOURCES:
            errors.append(f"config_source {self.config_source!r} is not one of {CONFIG_SOURCES}")
        if self.source_class not in SOURCE_CLASSES:
            errors.append(f"source_class {self.source_class!r} is not one of {SOURCE_CLASSES}")
        if self.source_class in MODELLED_CLASSES and not (self.model_ref or "").strip():
            errors.append(
                f"source_class is {self.source_class!r} but no model_ref — a run nobody "
                "can attribute to a model version cannot be reproduced or retired"
            )
        if self.ended_at is not None and self.ended_at < self.started_at:
            errors.append("ended_at is before started_at")

        seen: set[str] = set()
        for segment in self.segments:
            for problem in segment.validate():
                errors.append(f"segment {segment.segment_id}: {problem}")
            if segment.segment_id in seen:
                errors.append(f"segment id {segment.segment_id!r} appears twice")
            seen.add(segment.segment_id)
            if segment.started_at < self.started_at:
                errors.append(f"segment {segment.segment_id} starts before its run")
            if self.ended_at and segment.ended_at and segment.ended_at > self.ended_at:
                errors.append(f"segment {segment.segment_id} ends after its run")
        return errors


def _within(a: Any, b: Any, tolerance: float) -> bool:
    """Relative comparison, falling back to equality for non-numbers.

    Relative rather than absolute because the same tolerance has to serve a
    500 W setpoint and a 35 L/min flow rate.
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if a == b:
            return True
        scale = max(abs(float(a)), abs(float(b)))
        if scale == 0:
            return True
        return abs(float(a) - float(b)) / scale <= tolerance
    return a == b


@dataclass(frozen=True)
class RunMatch:
    """One candidate, and exactly why it matched."""

    run: Run
    matched: tuple[str, ...]
    #: Keys the reference declared that the candidate did not, or vice versa.
    #: Reported rather than ignored: a match on two keys out of six is a
    #: different statement from a match on two out of two.
    unshared: tuple[str, ...]

    @property
    def score(self) -> float:
        total = len(self.matched) + len(self.unshared)
        return len(self.matched) / total if total else 0.0


def similar_runs(
    reference: Run,
    candidates: Sequence[Run],
    *,
    keys: Sequence[str] | None = None,
    tolerance: float = 0.05,
    tolerances: Mapping[str, float] | None = None,
    include_simulated: bool = False,
) -> list[RunMatch]:
    """Candidates whose configuration matches *reference*, best first.

    ``keys`` restricts the comparison to the dimensions that actually matter
    for the question being asked — two runs at the same power but different
    flow rates are alike for one purpose and not for another, and the caller
    knows which. Default is every key the reference declares.

    A reference with no configuration returns nothing. Two runs are not alike
    because nothing is known about either, and returning everything would be a
    confident wrong answer in exactly the place a user is least able to check.

    Simulated runs are excluded unless asked for. A comparison that quietly
    includes synthetic runs is worse than one that finds no peers.
    """
    wanted = tuple(keys) if keys is not None else tuple(reference.config)
    if not wanted:
        return []

    per_key = dict(tolerances or {})
    matches: list[RunMatch] = []
    for candidate in candidates:
        if candidate.run_id == reference.run_id and candidate.site == reference.site:
            continue
        if candidate.source_class == "simulated" and not include_simulated:
            continue
        matched: list[str] = []
        unshared: list[str] = []
        for key in wanted:
            if key not in reference.config or key not in candidate.config:
                unshared.append(key)
                continue
            tol = per_key.get(key, tolerance)
            if _within(reference.config[key], candidate.config[key], tol):
                matched.append(key)
            else:
                unshared.append(key)
        if matched:
            matches.append(RunMatch(candidate, tuple(matched), tuple(unshared)))

    matches.sort(key=lambda m: (-m.score, -len(m.matched), m.run.site, m.run.run_id))
    return matches


__all__ = [
    "CONFIG_SOURCES",
    "GROUND_TRUTH_SOURCES",
    "LABEL_SOURCES",
    "RUNS_DDL",
    "SEGMENT_LABELS",
    "Run",
    "RunError",
    "RunMatch",
    "RunSegment",
    "similar_runs",
]
