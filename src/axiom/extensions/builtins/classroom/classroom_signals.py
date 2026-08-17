# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""SCAN classroom signal extractors (§2.9 / spec-classroom.md).

Deterministic rules over traces + cohort data that emit instructor-
facing signals. These feed SCAN's signal pipeline — SCAN handles the
routing, briefing compilation, and dashboard surface.

Signals emitted:
- student_stuck: many turns on same topic without resolution
- misconception_detected: content matches a known misconception pattern
- low_engagement: no interaction in N hours during active period
- high_engagement: turn count >N stdev above cohort mean
- objective_gap: LO coverage <threshold across cohort

Standalone: all rules are pure functions over trace dicts.
Federation stretch: signals can be merged across nodes (each signal
carries student_id + signal_type; a hub can dedupe + aggregate).
"""

from __future__ import annotations

import statistics
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from axiom.memory.composition import CompositionService
    from axiom.memory.fragment import MemoryFragment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_ts(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _base_signal(
    signal_type: str,
    student_id: str,
    severity: str = "medium",
    **extra: Any,
) -> dict:
    s = {
        "signal_type": signal_type,
        "student_id": student_id,
        "emitted_at": _now_iso(),
        "severity": severity,
    }
    s.update(extra)
    return s


# ---------------------------------------------------------------------------
# student_stuck
# ---------------------------------------------------------------------------


def detect_student_stuck(traces: list[dict], threshold: int = 5) -> list[dict]:
    """Flag students with >threshold turns on the same topic."""
    # Count turns per (student, topic)
    counts: dict[tuple[str, str], int] = {}
    for t in traces:
        sid = t.get("student_id")
        if not sid:
            continue
        for topic in t.get("topics", []):
            counts[(sid, topic)] = counts.get((sid, topic), 0) + 1

    signals = []
    for (sid, topic), cnt in counts.items():
        if cnt > threshold:
            signals.append(_base_signal(
                "student_stuck",
                student_id=sid,
                severity="medium",
                topic=topic,
                turn_count=cnt,
            ))
    return signals


# ---------------------------------------------------------------------------
# misconception_detected
# ---------------------------------------------------------------------------


def detect_misconceptions(
    traces: list[dict],
    patterns: list[dict],
) -> list[dict]:
    """Match trace content against known misconception keyword sets."""
    signals = []
    for t in traces:
        content = str(t.get("content", "")).lower()
        if not content:
            continue
        for p in patterns:
            keywords = [k.lower() for k in p.get("keywords", [])]
            if any(k in content for k in keywords):
                signals.append(_base_signal(
                    "misconception_detected",
                    student_id=t.get("student_id", ""),
                    severity="high",
                    misconception_id=p["id"],
                    note=p.get("note", ""),
                    trace_id=t.get("trace_id"),
                ))
                break  # one match per trace is enough
    return signals


# ---------------------------------------------------------------------------
# low_engagement
# ---------------------------------------------------------------------------


def detect_low_engagement(
    traces: list[dict],
    now: datetime,
    inactive_hours: int,
    active_student_ids: list[str],
) -> list[dict]:
    """Flag students in active_student_ids with no recent traces."""
    last_seen: dict[str, datetime] = {}
    for t in traces:
        sid = t.get("student_id")
        if not sid:
            continue
        ts = _parse_ts(t["timestamp"])
        if sid not in last_seen or ts > last_seen[sid]:
            last_seen[sid] = ts

    signals = []
    cutoff = now - _timedelta_hours(inactive_hours)
    for sid in active_student_ids:
        latest = last_seen.get(sid)
        if latest is None or latest < cutoff:
            hours_since = None
            if latest is not None:
                hours_since = (now - latest).total_seconds() / 3600
            signals.append(_base_signal(
                "low_engagement",
                student_id=sid,
                severity="medium",
                hours_since_last_activity=hours_since,
                last_activity=latest.isoformat() if latest else None,
            ))
    return signals


def _timedelta_hours(n: int):
    from datetime import timedelta

    return timedelta(hours=n)


# ---------------------------------------------------------------------------
# high_engagement
# ---------------------------------------------------------------------------


def detect_high_engagement(
    traces: list[dict],
    stdev_threshold: float = 2.0,  # kept for API compat; unused
    median_multiplier: float = 2.5,
) -> list[dict]:
    """Flag students whose turn count exceeds median_multiplier × cohort median.

    Uses median-based detection rather than mean+stdev because turn
    counts are heavily right-skewed: one very active student inflates
    both mean and stdev, masking their own outlier status. Median is
    robust; a cutoff of 2.5× median matches how instructors think
    ("twice the class average" ≈ "heavily engaged").
    """
    turn_counts: dict[str, int] = {}
    for t in traces:
        sid = t.get("student_id")
        if sid:
            turn_counts[sid] = turn_counts.get(sid, 0) + 1

    counts = list(turn_counts.values())
    if len(counts) < 2:
        return []

    median = statistics.median(counts)
    if median == 0:
        return []
    cutoff = median_multiplier * median

    signals = []
    for sid, cnt in turn_counts.items():
        if cnt > cutoff:
            signals.append(_base_signal(
                "high_engagement",
                student_id=sid,
                severity="low",  # info-level — high engagement often positive
                turn_count=cnt,
                cohort_median=median,
                multiple_of_median=cnt / median,
            ))
    return signals


# ---------------------------------------------------------------------------
# objective_gap
# ---------------------------------------------------------------------------


def detect_objective_gaps(
    traces: list[dict],
    learning_objectives: list[dict],
    student_ids: list[str],
    coverage_threshold: float = 0.2,
) -> list[dict]:
    """Flag learning objectives with cohort coverage <threshold."""
    total = len(student_ids)
    if total == 0:
        return []

    # Per-LO: set of student_ids who touched it
    coverage: dict[str, set[str]] = {lo["id"]: set() for lo in learning_objectives}
    for t in traces:
        sid = t.get("student_id")
        if not sid or sid not in student_ids:
            continue
        for topic in t.get("topics", []):
            if topic in coverage:
                coverage[topic].add(sid)

    signals = []
    for lo in learning_objectives:
        lo_id = lo["id"]
        covered = len(coverage[lo_id])
        frac = covered / total
        if frac < coverage_threshold:
            signals.append({
                "signal_type": "objective_gap",
                "emitted_at": _now_iso(),
                "severity": "medium",
                "objective_id": lo_id,
                "objective_title": lo.get("title", ""),
                "coverage_fraction": frac,
                "students_covered": covered,
                "students_total": total,
            })
    return signals


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------


def detect_all_signals(
    traces: list[dict],
    learning_objectives: list[dict],
    misconception_patterns: list[dict],
    active_student_ids: list[str],
    now: datetime,
    stuck_threshold: int = 5,
    inactive_hours: int = 48,
    stdev_threshold: float = 2.0,
    coverage_threshold: float = 0.2,
) -> list[dict]:
    """Run all detectors in one pass."""
    signals = []
    signals.extend(detect_student_stuck(traces, threshold=stuck_threshold))
    signals.extend(detect_misconceptions(traces, patterns=misconception_patterns))
    signals.extend(detect_low_engagement(
        traces, now=now, inactive_hours=inactive_hours,
        active_student_ids=active_student_ids,
    ))
    signals.extend(detect_high_engagement(traces, stdev_threshold=stdev_threshold))
    signals.extend(detect_objective_gaps(
        traces, learning_objectives=learning_objectives,
        student_ids=active_student_ids, coverage_threshold=coverage_threshold,
    ))
    return signals


# ---------------------------------------------------------------------------
# Composition integration (#73)
# ---------------------------------------------------------------------------


def record_signal(
    composition: CompositionService,
    signal: dict,
    classroom_id: str,
    instructor_id: str,
) -> MemoryFragment:
    """Materialize an SCAN classroom signal as MemoryFragment(episodic).

    Ownership: instructor is master (they act on signals); SCAN agent
    gets EFFORT delegation to keep emitting related signals. Per ADR-026.

    Signals are episodic — they reference a point-in-time observation,
    not a persistent fact. `signal_type`, `subject` (student_id), and
    `emitted_at` anchor the record.
    """
    from axiom.memory.ownership import (
        Right,
        new_ownership,
    )
    from axiom.memory.ownership import (
        delegate as _delegate,
    )

    own = new_ownership(master=instructor_id)
    own = _delegate(
        own,
        delegate_principal="scan",
        rights={Right.EFFORT},
        expires_at="2099-12-31T23:59:59Z",
    )

    # Episodic fragments require event_time in content
    content = dict(signal)
    content.setdefault("event_time", signal.get("emitted_at",
                                                 datetime.now(UTC).isoformat()))
    content["classroom_id"] = classroom_id

    return composition.write(
        content=content,
        cognitive_type="episodic",
        principal_id=instructor_id,
        agents={"scan"},
        resources={f"classroom:{classroom_id}", "signal-feed"},
        ownership=own,
    )
