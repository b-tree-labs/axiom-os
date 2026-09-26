# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Trace export — CSV (standalone) + optional Parquet + anonymization.

Per spec-classroom.md §2.11 + prd §5.10. Builds research-ready
datasets from traces, metrics, quiz responses.

Standalone-first: CSV always works (stdlib). Parquet is optional
and gated on pyarrow availability with an informative error.

Anonymization: deterministic pseudonyms via sha256 truncation. Two
exports of the same student_id produce the same pseudonym (critical
for researchers tracking longitudinal data), but the pseudonym is
not reversible to the real id without access to the hash input.

Consent filter: caller passes `consented_student_ids`; only those
students' rows are exported. Handles IRB / GDPR opt-out without
mutating upstream data.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from pathlib import Path

from axiom.medallion.export import (
    consent_filter as _core_consent_filter,
)
from axiom.medallion.export import (
    maybe_pseudonymize as _maybe_anon,
)
from axiom.medallion.export import (
    # Re-exported for research_bundle.py and tests. Aliased `as pseudonymize`
    # to signal ruff this is intentional public API, not an unused import.
    pseudonymize as pseudonymize,
)

from .quiz_scoring import ScoredResponse

# ---------------------------------------------------------------------------
# Consent filter — classroom shim (uses student_id as the id key)
# ---------------------------------------------------------------------------


def _consent_filter(
    rows: Iterable[dict],
    consented_student_ids: set[str] | None,
) -> list[dict]:
    return _core_consent_filter(
        rows, consented_ids=consented_student_ids, id_key="student_id"
    )


# ---------------------------------------------------------------------------
# Trace flattening
# ---------------------------------------------------------------------------


_TRACE_COLUMNS = [
    "trace_id", "student_id", "session_id", "session_type",
    "turn_index", "timestamp", "prompt_tokens", "completion_tokens",
    "total_tokens", "rag_results_count", "labels", "topics",
]


def _flatten_trace(t: dict, anonymize: bool) -> dict:
    tokens = t.get("tokens", {}) or {}
    prompt = tokens.get("prompt", 0)
    completion = tokens.get("completion", 0)
    return {
        "trace_id": t.get("trace_id", ""),
        "student_id": _maybe_anon(t.get("student_id", ""), anonymize),
        "session_id": t.get("session_id", ""),
        "session_type": t.get("session_type", ""),
        "turn_index": t.get("turn_index", 0),
        "timestamp": t.get("timestamp", ""),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "rag_results_count": t.get("rag_results_count", 0),
        "labels": ",".join(t.get("labels", [])),
        "topics": ",".join(t.get("topics", [])),
    }


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


def export_traces_csv(
    traces: list[dict],
    path: Path,
    anonymize: bool = False,
    consented_student_ids: set[str] | None = None,
) -> None:
    """Write traces to a CSV file. Standalone — no external deps."""
    filtered = _consent_filter(traces, consented_student_ids)
    # Apply anonymization AFTER consent filter (consent checks real ids)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_TRACE_COLUMNS)
        writer.writeheader()
        for t in filtered:
            writer.writerow(_flatten_trace(t, anonymize))


# ---------------------------------------------------------------------------
# Parquet export (optional)
# ---------------------------------------------------------------------------


def export_traces_parquet(
    traces: list[dict],
    path: Path,
    anonymize: bool = False,
    consented_student_ids: set[str] | None = None,
) -> None:
    """Write traces to a Parquet file. Requires pyarrow."""
    import sys

    pyarrow = sys.modules.get("pyarrow")
    if pyarrow is None:
        try:
            import pyarrow as _pa  # noqa: F401
            import pyarrow.parquet as _pq  # noqa: F401

            pyarrow = sys.modules.get("pyarrow")
        except ImportError:
            pyarrow = None

    if pyarrow is None:
        raise RuntimeError(
            "pyarrow is required for Parquet export. "
            "Install with: pip install pyarrow"
        )

    import pyarrow as pa
    import pyarrow.parquet as pq

    filtered = _consent_filter(traces, consented_student_ids)
    rows = [_flatten_trace(t, anonymize) for t in filtered]
    if not rows:
        # Empty dataset — write empty table
        table = pa.table({col: [] for col in _TRACE_COLUMNS})
    else:
        cols = {col: [r[col] for r in rows] for col in _TRACE_COLUMNS}
        table = pa.table(cols)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)


# ---------------------------------------------------------------------------
# Research dataset — traces + quiz + metrics combined
# ---------------------------------------------------------------------------


_QUIZ_COLUMNS = [
    "kind", "student_id", "assessment_id", "question_id", "question_type",
    "final_score", "reviewed_by",
]


def export_research_dataset(
    traces: list[dict],
    quiz_responses: list[ScoredResponse],
    path: Path,
    anonymize: bool = False,
    consented_student_ids: set[str] | None = None,
) -> None:
    """Emit a combined CSV with traces + quiz rows distinguished by `kind`.

    Researchers get one file with: trace rows (kind=trace) and
    quiz rows (kind=quiz). Easier downstream join than two files.
    """
    trace_rows_raw = _consent_filter(traces, consented_student_ids)
    quiz_rows_raw = [
        q for q in quiz_responses
        if consented_student_ids is None or q.student_id in consented_student_ids
    ]

    # Unified column set
    columns = ["kind"] + _TRACE_COLUMNS + [
        "assessment_id", "question_id", "question_type",
        "final_score", "reviewed_by",
    ]

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()

        for t in trace_rows_raw:
            row = {"kind": "trace"}
            row.update(_flatten_trace(t, anonymize))
            writer.writerow(row)

        for q in quiz_rows_raw:
            writer.writerow({
                "kind": "quiz",
                "student_id": _maybe_anon(q.student_id, anonymize),
                "assessment_id": q.assessment_id,
                "question_id": q.question_id,
                "question_type": q.question_type,
                "final_score": q.final_score,
                "reviewed_by": q.reviewed_by or "",
            })
