# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Research bundle — IRB-aware dataset packaging.

Builds on trace_export.py to produce a complete research bundle:
- traces.csv: per-turn records + labels + topics
- quiz.csv: per-question scored responses
- interviews.csv: questionnaire responses (begin/mid/end interviews)
- manifest.json: IRB protocol id, retention policy, consent version,
  consented student count, pseudonymization flag, built_at

Standalone-first. Consent-gated: only students in
`consented_student_ids` are exported. Pseudonym consistency is
guaranteed across all artifacts (same student → same anon id
in every file).

Spec: prd-classroom.md §5.10.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from .quiz_scoring import ScoredResponse
from .trace_export import (
    _TRACE_COLUMNS,
    _flatten_trace,
    pseudonymize,
)

# ---------------------------------------------------------------------------
# Interview CSV
# ---------------------------------------------------------------------------


_INTERVIEW_COLUMNS = [
    "session_id", "student_id", "instrument_id", "question_id",
    "response", "timestamp",
]


def _write_interview_csv(
    interview_responses: Iterable[dict],
    path: Path,
    consented: set[str] | None,
    anonymize: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [r for r in interview_responses
            if consented is None or r.get("student_id") in consented]

    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_INTERVIEW_COLUMNS)
        writer.writeheader()
        for r in rows:
            out = {col: r.get(col, "") for col in _INTERVIEW_COLUMNS}
            if anonymize and out["student_id"]:
                out["student_id"] = pseudonymize(out["student_id"])
            writer.writerow(out)


# ---------------------------------------------------------------------------
# Quiz CSV
# ---------------------------------------------------------------------------


_QUIZ_COLUMNS = [
    "student_id", "assessment_id", "question_id", "question_type",
    "final_score", "suggested_score", "reviewed_by", "needs_review",
]


def _write_quiz_csv(
    quiz_responses: list[ScoredResponse],
    path: Path,
    consented: set[str] | None,
    anonymize: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [q for q in quiz_responses
            if consented is None or q.student_id in consented]

    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_QUIZ_COLUMNS)
        writer.writeheader()
        for q in rows:
            sid = pseudonymize(q.student_id) if anonymize else q.student_id
            writer.writerow({
                "student_id": sid,
                "assessment_id": q.assessment_id,
                "question_id": q.question_id,
                "question_type": q.question_type,
                "final_score": q.final_score,
                "suggested_score": q.suggested_score,
                "reviewed_by": q.reviewed_by or "",
                "needs_review": q.needs_review,
            })


# ---------------------------------------------------------------------------
# Traces CSV (reuses trace_export internals, directly)
# ---------------------------------------------------------------------------


def _write_traces_csv(
    traces: list[dict],
    path: Path,
    consented: set[str] | None,
    anonymize: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [t for t in traces
            if consented is None or t.get("student_id") in consented]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_TRACE_COLUMNS)
        writer.writeheader()
        for t in rows:
            writer.writerow(_flatten_trace(t, anonymize))


# ---------------------------------------------------------------------------
# Bundle builder
# ---------------------------------------------------------------------------


def build_research_bundle(
    out_dir: Path,
    classroom_id: str,
    traces: list[dict],
    quiz_responses: list[ScoredResponse],
    interview_responses: list[dict],
    irb_protocol: str,
    retention_years: int,
    consent_version: str,
    consented_student_ids: set[str],
    anonymize: bool = True,
) -> None:
    """Write a research bundle to `out_dir`.

    Bundle contents:
      traces.csv, quiz.csv, interviews.csv, manifest.json

    Only students in `consented_student_ids` are included. Pseudonym
    consistency across all three CSVs is guaranteed by the
    deterministic pseudonymize() function.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    _write_traces_csv(traces, out_dir / "traces.csv",
                      consented_student_ids, anonymize)
    _write_quiz_csv(quiz_responses, out_dir / "quiz.csv",
                    consented_student_ids, anonymize)
    _write_interview_csv(interview_responses, out_dir / "interviews.csv",
                         consented_student_ids, anonymize)

    manifest = {
        "classroom_id": classroom_id,
        "irb_protocol": irb_protocol,
        "retention_years": retention_years,
        "consent_version": consent_version,
        "consented_student_count": len(consented_student_ids),
        "anonymized": anonymize,
        "built_at": datetime.now(UTC).isoformat(),
        "artifacts": ["traces.csv", "quiz.csv", "interviews.csv"],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
