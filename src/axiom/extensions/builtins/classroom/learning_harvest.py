# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Per-student learning harvest (§5.9 / spec-classroom.md).

At course completion, each student receives a `.axiompack` —
a portable ZIP bundle with their own learning record:
- manifest.json: course + classroom context, signer_node, built_at
- traces.json: chat transcripts (only this student's)
- quiz.json: scored responses (only this student's)
- notes.json: personal notes captured during the course

Distinct from research_bundle (instructor-facing, multi-student,
IRB-gated). Harvest is student-facing, single-student, signed by
the course for portability.

Federation stretch (§5.9): harvest can propose peer-reviewed
promotion of high-quality student content into the course RAG
for future cohorts. `propose_promotion_candidates` surfaces the
candidates; CURIO / peer review is the federation workflow.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .quiz_scoring import ScoredResponse

# ---------------------------------------------------------------------------
# Single-student harvest
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _filter_by_student(items: Iterable[dict], student_id: str) -> list[dict]:
    return [i for i in items if i.get("student_id") == student_id]


def build_student_harvest(
    out_path: Path,
    student_id: str,
    classroom_id: str,
    course_id: str,
    course_version: str,
    traces: list[dict],
    quiz_responses: list[ScoredResponse],
    notes: list[dict],
    signer_node: str,
) -> Path:
    """Write one student's .axiompack bundle to out_path."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    own_traces = _filter_by_student(traces, student_id)
    own_quiz = [
        {"student_id": q.student_id, "assessment_id": q.assessment_id,
         "question_id": q.question_id, "question_type": q.question_type,
         "final_score": q.final_score, "reviewed_by": q.reviewed_by}
        for q in quiz_responses if q.student_id == student_id
    ]
    own_notes = _filter_by_student(notes, student_id)

    manifest = {
        "format": "axiompack/student-harvest/v1",
        "student_id": student_id,
        "classroom_id": classroom_id,
        "course_id": course_id,
        "course_version": course_version,
        "signer_node": signer_node,
        "built_at": _now_iso(),
        "signature": None,  # federation layer signs before distribution
        "counts": {
            "traces": len(own_traces),
            "quiz": len(own_quiz),
            "notes": len(own_notes),
        },
    }

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr("traces.json", json.dumps(own_traces, indent=2))
        zf.writestr("quiz.json", json.dumps(own_quiz, indent=2))
        zf.writestr("notes.json", json.dumps(own_notes, indent=2))

    return out_path


# ---------------------------------------------------------------------------
# Cohort harvest (batch builds per-student bundles)
# ---------------------------------------------------------------------------


@dataclass
class CohortHarvestResult:
    classroom_id: str
    bundles: dict[str, Path] = field(default_factory=dict)  # student_id → path


def build_cohort_harvest(
    out_dir: Path,
    classroom_id: str,
    course_id: str,
    course_version: str,
    student_ids: list[str],
    traces: list[dict],
    quiz_responses: list[ScoredResponse],
    notes: list[dict],
    signer_node: str,
) -> CohortHarvestResult:
    """Build .axiompack bundles for every student in the cohort."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = CohortHarvestResult(classroom_id=classroom_id)

    for sid in student_ids:
        path = out_dir / f"{sid}.axiompack"
        build_student_harvest(
            out_path=path,
            student_id=sid,
            classroom_id=classroom_id,
            course_id=course_id,
            course_version=course_version,
            traces=traces,
            quiz_responses=quiz_responses,
            notes=notes,
            signer_node=signer_node,
        )
        result.bundles[sid] = path
    return result


# ---------------------------------------------------------------------------
# Promotion candidates (federation §5.9)
# ---------------------------------------------------------------------------


def propose_promotion_candidates(
    notes: list[dict],
    threshold: float = 0.7,
) -> list[dict]:
    """Surface student notes whose quality_score >= threshold as
    promotion candidates.

    CURIO + peer review (federation layer) process these to decide
    whether to fold the note into the course RAG for future cohorts.
    Each candidate carries `promotion_status="proposed"`; status
    transitions (accepted/rejected) happen in the federation workflow.
    """
    candidates = []
    for n in notes:
        if n.get("quality_score", 0.0) >= threshold:
            candidates.append({
                **n,
                "promotion_status": "proposed",
                "proposed_at": _now_iso(),
            })
    return candidates
