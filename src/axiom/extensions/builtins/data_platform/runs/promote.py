# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Promote pushed event rows into runs and their labelled segments.

Signals conform into ``silver.signals``; these do not. A curated event is not a
measurement at an instant, it is a *judgement about an interval*, and giving it
the signal shape would mean inventing a value for it. So runs get their own
promotion rather than being bent through the signal normalizer — the same
reason a run is a separate table and not a column on a reading.

The shape a publisher sends is deliberately close to what a person actually
records, so the edge does no interpretation:

    {"site": "ut-triga", "run_id": "2026-09-14-A",
     "segment_id": "seg-003", "label": "steady_state",
     "started_at": "...", "ended_at": "...",
     "label_source": "curated", "labelled_by": "nick",
     "notes": "held at 900 kW"}

**A row that cannot be admitted is rejected and named.** Never defaulted: the
one thing this path exists to preserve is that a human marked this window, and
quietly filling in a missing ``labelled_by`` with "unknown" would destroy
exactly the fact being carried. A rejected row is visible and fixable at the
publisher; a defaulted one is neither.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import Run, RunSegment


@dataclass(frozen=True)
class Rejected:
    row: Mapping[str, Any]
    reasons: tuple[str, ...]

    @property
    def where(self) -> str:
        site = self.row.get("site", "?")
        run = self.row.get("run_id", "?")
        seg = self.row.get("segment_id", "?")
        return f"{site}/{run}/{seg}"


@dataclass
class Promotion:
    """What a batch of pushed rows became."""

    runs: dict[tuple[str, str], Run] = field(default_factory=dict)
    rejected: list[Rejected] = field(default_factory=list)

    @property
    def rows_in(self) -> int:
        return sum(len(r.segments) for r in self.runs.values()) + len(self.rejected)

    @property
    def segments(self) -> list[RunSegment]:
        return [s for run in self.runs.values() for s in run.segments]

    @property
    def ok(self) -> bool:
        return not self.rejected

    def report(self) -> list[str]:
        lines = [
            f"promoted {len(self.segments)} segment(s) across {len(self.runs)} run(s)"
        ]
        for bad in self.rejected:
            lines.append(f"REJECTED {bad.where}: {'; '.join(bad.reasons)}")
        return lines


def _instant(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None
    return None


def promote_segments(rows: Iterable[Mapping[str, Any]]) -> Promotion:
    """Turn pushed rows into runs with segments, rejecting what will not hold.

    Runs are created implicitly from the segments that reference them: a
    publisher recording "this window of run X was steady" should not also have
    to have declared run X first, and requiring it would mean the first push
    after a run starts silently drops its own segments.

    The run's span is widened to contain its segments rather than asserted,
    because the publisher knows when it labelled something and may not know
    when the run ends — an open run is the normal case while it is happening.
    """
    out = Promotion()
    staged: dict[tuple[str, str], list[RunSegment]] = {}
    spans: dict[tuple[str, str], tuple[datetime, datetime | None]] = {}
    configs: dict[tuple[str, str], dict[str, Any]] = {}

    for row in rows:
        problems: list[str] = []
        site = str(row.get("site") or "").strip()
        run_id = str(row.get("run_id") or "").strip()
        if not site:
            problems.append("no site")
        if not run_id:
            problems.append("no run_id")

        started = _instant(row.get("started_at"))
        if started is None:
            problems.append(f"started_at {row.get('started_at')!r} is not a timestamp")
        ended = _instant(row.get("ended_at")) if row.get("ended_at") else None
        if row.get("ended_at") and ended is None:
            problems.append(f"ended_at {row.get('ended_at')!r} is not a timestamp")

        if problems:
            out.rejected.append(Rejected(row, tuple(problems)))
            continue

        confidence = row.get("confidence")
        segment = RunSegment(
            segment_id=str(row.get("segment_id") or "").strip(),
            label=str(row.get("label") or ""),
            started_at=started,  # type: ignore[arg-type]
            ended_at=ended,
            label_source=str(row.get("label_source") or ""),
            labelled_by=(str(row["labelled_by"]).strip() or None)
            if row.get("labelled_by") is not None else None,
            confidence=float(confidence) if confidence is not None else None,
            notes=(str(row["notes"]) if row.get("notes") is not None else None),
        )
        errors = segment.validate()
        if errors:
            out.rejected.append(Rejected(row, tuple(errors)))
            continue

        key = (site, run_id)
        staged.setdefault(key, []).append(segment)
        low, high = spans.get(key, (segment.started_at, segment.ended_at))
        low = min(low, segment.started_at)
        if segment.ended_at is None or high is None:
            high = None  # an open segment leaves the run open
        else:
            high = max(high, segment.ended_at)
        spans[key] = (low, high)
        if isinstance(row.get("config"), Mapping):
            configs.setdefault(key, {}).update(dict(row["config"]))

    for key, segments in staged.items():
        site, run_id = key
        started, ended = spans[key]
        seen: set[str] = set()
        unique: list[RunSegment] = []
        for segment in sorted(segments, key=lambda s: (s.started_at, s.segment_id)):
            if segment.segment_id in seen:
                out.rejected.append(Rejected(
                    {"site": site, "run_id": run_id, "segment_id": segment.segment_id},
                    ("segment id appears twice in one batch — ids are how a "
                     "correction finds the row it replaces",),
                ))
                continue
            seen.add(segment.segment_id)
            unique.append(segment)
        if not unique:
            continue
        out.runs[key] = Run(
            site=site,
            run_id=run_id,
            started_at=started,
            ended_at=ended,
            config=configs.get(key, {}),
            config_source="declared",
            segments=tuple(unique),
        )
    return out


#: Idempotent by ``(site, run_id, segment_id)``. A re-push of a corrected label
#: replaces the old one rather than accumulating both — a curator changing their
#: mind is the expected case, not an anomaly.
UPSERT_SEGMENT = """
INSERT INTO silver.run_segments
  (site, run_id, segment_id, label, started_at, ended_at,
   label_source, labelled_by, confidence, notes)
VALUES (%(site)s, %(run_id)s, %(segment_id)s, %(label)s, %(started_at)s,
        %(ended_at)s, %(label_source)s, %(labelled_by)s, %(confidence)s, %(notes)s)
ON CONFLICT (site, run_id, segment_id) DO UPDATE SET
  label = excluded.label,
  started_at = excluded.started_at,
  ended_at = excluded.ended_at,
  label_source = excluded.label_source,
  labelled_by = excluded.labelled_by,
  confidence = excluded.confidence,
  notes = excluded.notes
"""

#: The run row. ``started_at`` only ever moves earlier and ``ended_at`` later,
#: because a later push carrying one segment must not shrink a run it only
#: partially describes.
UPSERT_RUN = """
INSERT INTO silver.runs (site, run_id, started_at, ended_at, config, config_source)
VALUES (%(site)s, %(run_id)s, %(started_at)s, %(ended_at)s,
        %(config)s::jsonb, %(config_source)s)
ON CONFLICT (site, run_id) DO UPDATE SET
  started_at = LEAST(silver.runs.started_at, excluded.started_at),
  ended_at = CASE
      WHEN silver.runs.ended_at IS NULL OR excluded.ended_at IS NULL THEN NULL
      ELSE GREATEST(silver.runs.ended_at, excluded.ended_at)
  END,
  config = silver.runs.config || excluded.config,
  config_source = excluded.config_source
"""


__all__ = ["Promotion", "Rejected", "UPSERT_RUN", "UPSERT_SEGMENT", "promote_segments"]
