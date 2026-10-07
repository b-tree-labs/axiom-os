# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Classroom metrics aggregation (§5.4).

Transforms raw traces (per-turn records emitted by ClassroomTracer
or any trace provider) into instructor-actionable metrics.

Per-student: sessions, turns, token consumption, RAG hit rate,
topic distribution, label distribution, session-type breakdown.

Per-cohort: totals + means + outliers + per-student rollup.

Time-series: weekly binning for trend analysis.

Standalone-first: aggregation is pure data-in-data-out; no external
deps. Federation stretch (ADR-023): metrics summaries serialize as
signed claims so a hub node can merge cross-node cohort views
without pulling raw traces.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

# ---------------------------------------------------------------------------
# Per-student metrics
# ---------------------------------------------------------------------------


def per_student_metrics(traces: list[dict], student_id: str) -> dict:
    """Aggregate all metrics for one student."""
    own = [t for t in traces if t.get("student_id") == student_id]

    sessions = {t.get("session_id") for t in own if t.get("session_id")}
    turns = len(own)

    prompt_tokens = sum(t.get("tokens", {}).get("prompt", 0) for t in own)
    completion_tokens = sum(t.get("tokens", {}).get("completion", 0) for t in own)

    rag_hits = sum(1 for t in own if t.get("rag_results_count", 0) > 0)
    rag_hit_rate = (rag_hits / turns) if turns else None

    labels: dict[str, int] = {}
    for t in own:
        for label in t.get("labels", []):
            labels[label] = labels.get(label, 0) + 1

    topics: dict[str, int] = {}
    for t in own:
        for topic in t.get("topics", []):
            topics[topic] = topics.get(topic, 0) + 1

    session_types: dict[str, int] = {}
    seen_sessions: set[str] = set()
    for t in own:
        sid = t.get("session_id")
        stype = t.get("session_type")
        if sid and stype and sid not in seen_sessions:
            session_types[stype] = session_types.get(stype, 0) + 1
            seen_sessions.add(sid)

    return {
        "student_id": student_id,
        "sessions": len(sessions),
        "turns": turns,
        "tokens": {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "total": prompt_tokens + completion_tokens,
        },
        "rag_hit_rate": rag_hit_rate,
        "labels": labels,
        "topics": topics,
        "session_types": session_types,
    }


# ---------------------------------------------------------------------------
# Cohort metrics
# ---------------------------------------------------------------------------


def cohort_metrics(traces: list[dict]) -> dict:
    """Aggregate cohort-level metrics + per-student rollup."""
    student_ids = sorted({t.get("student_id") for t in traces if t.get("student_id")})
    per_student = [
        per_student_metrics(traces, sid) for sid in student_ids
    ]

    total_sessions = sum(m["sessions"] for m in per_student)
    total_turns = sum(m["turns"] for m in per_student)

    mean_turns = (
        statistics.fmean(m["turns"] for m in per_student) if per_student else 0.0
    )
    mean_sessions = (
        statistics.fmean(m["sessions"] for m in per_student) if per_student else 0.0
    )

    rag_rates = [m["rag_hit_rate"] for m in per_student if m["rag_hit_rate"] is not None]
    mean_rag_hit = statistics.fmean(rag_rates) if rag_rates else None

    return {
        "total_students": len(student_ids),
        "total_sessions": total_sessions,
        "total_turns": total_turns,
        "mean_turns_per_student": mean_turns,
        "mean_sessions_per_student": mean_sessions,
        "mean_rag_hit_rate": mean_rag_hit,
        "students": per_student,
    }


# ---------------------------------------------------------------------------
# Time-series binning
# ---------------------------------------------------------------------------


def _parse_ts(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _week_start(dt: datetime) -> datetime:
    """Return the Monday 00:00 UTC of the week containing dt."""
    start_of_day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_of_day - timedelta(days=start_of_day.weekday())


def weekly_turns(traces: list[dict], student_id: str) -> list[dict]:
    """Bin this student's turns by week. Returns list sorted by week_start."""
    own = [t for t in traces if t.get("student_id") == student_id]
    if not own:
        return []

    bins: dict[datetime, int] = {}
    for t in own:
        ts = _parse_ts(t["timestamp"])
        wk = _week_start(ts)
        bins[wk] = bins.get(wk, 0) + 1

    return [
        {"week_start": wk.isoformat(), "turns": count}
        for wk, count in sorted(bins.items())
    ]


# ---------------------------------------------------------------------------
# Federation claim serialization (ADR-023)
# ---------------------------------------------------------------------------


def serialize_metrics_claim(
    metrics: dict,
    signer_node: str,
    classroom_id: str,
) -> dict:
    """Produce a signed-claim dict for cross-node metric transport."""
    return {
        "student_id": metrics["student_id"],
        "classroom_id": classroom_id,
        "signer_node": signer_node,
        "issued_at": datetime.now(UTC).isoformat(),
        "metrics": metrics,
        "signature": None,  # federation layer fills in
    }


def merge_metrics_claims(
    claims: Iterable[dict],
    trust_verifier: Callable[[dict], bool],
) -> dict:
    """Merge per-student metric claims from multiple nodes into a cohort view.

    Claims that fail verification are rejected — their metrics are
    excluded from the cohort view, and the count is reported.
    """
    accepted: list[dict] = []
    rejected = 0
    for claim in claims:
        if trust_verifier(claim):
            accepted.append(claim["metrics"])
        else:
            rejected += 1

    total_sessions = sum(m.get("sessions", 0) for m in accepted)
    total_turns = sum(m.get("turns", 0) for m in accepted)

    return {
        "total_students": len(accepted),
        "total_sessions": total_sessions,
        "total_turns": total_turns,
        "rejected": rejected,
        "students": accepted,
    }
