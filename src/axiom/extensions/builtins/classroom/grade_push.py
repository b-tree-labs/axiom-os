# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Grade push — batch grade distribution (standalone + LMS + federation-aware).

Per spec-classroom.md §3.1 WF-4.

Three modes:
1. **Standalone (default)**: write per-student grades to a local ledger
   at runtime/classrooms/<id>/grades/<assessment>.json. No network.
2. **LMS (optional)**: additionally push each per-student aggregate to
   the connected LMS (Canvas), capturing partial failures.
3. **Federation (stretch)**: accept cross-node signed grade claims
   (ADR-023) from peer nodes before aggregating + pushing.

The local ledger is authoritative — it always persists, even when
LMS push succeeds. That way, grade history survives LMS outages,
credential expiry, or federation trust revocation. Re-push from the
ledger is always possible.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .quiz_scoring import ScoredResponse, per_student_grades

if TYPE_CHECKING:
    from axiom.memory.composition import CompositionService
    from axiom.memory.fragment import MemoryFragment


# ---------------------------------------------------------------------------
# LMS protocol (duck-typed; CanvasLMSProvider implements this)
# ---------------------------------------------------------------------------


class LMSGradePush:
    """Protocol for LMS grade push. Duck-typed."""

    def push_grade(
        self,
        course_id: str,
        assignment_id: str,
        student_id: str,
        score: float,
        comment: str = "",
    ) -> Any: ...


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class GradePushBatchResult:
    """Summary of a batch grade push."""

    assessment_id: str
    classroom_id: str
    pushed_count: int = 0           # successfully pushed to LMS
    local_logged_count: int = 0     # written to local ledger
    skipped_count: int = 0          # ungraded or in-queue responses
    rejected_count: int = 0         # untrusted federation claims
    failures: list[dict] = field(default_factory=list)
    ledger_path: str | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _ledger_path(local_dir: Path, classroom_id: str, assessment_id: str) -> Path:
    return local_dir / "classrooms" / classroom_id / "grades" / f"{assessment_id}.json"


def _write_ledger(
    local_dir: Path,
    classroom_id: str,
    assessment_id: str,
    grades: dict[str, dict],
) -> Path:
    path = _ledger_path(local_dir, classroom_id, assessment_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "classroom_id": classroom_id,
        "assessment_id": assessment_id,
        "generated_at": datetime.now(UTC).isoformat(),
        "grades": [
            {"student_id": sid, **info}
            for sid, info in sorted(grades.items())
        ],
    }
    path.write_text(json.dumps(payload, indent=2))
    return path


# ---------------------------------------------------------------------------
# push_grades — from ScoredResponse list
# ---------------------------------------------------------------------------


def push_grades(
    scored_responses: list[ScoredResponse],
    assessment_id: str,
    classroom_id: str,
    canvas_course_id: str | None,
    canvas_assignment_id: str | None,
    lms: LMSGradePush | None,
    local_dir: Path,
) -> GradePushBatchResult:
    """Aggregate per-student grades for one assessment and push.

    Always writes to the local ledger. Also pushes to LMS if provided.
    Skips ungraded / in-queue responses (they should be resolved via
    override_score first).
    """
    # Count skipped (ungraded or in-queue) for the target assessment
    skipped = sum(
        1 for r in scored_responses
        if r.assessment_id == assessment_id
        and (r.final_score is None or r.needs_review)
    )

    aggregated = per_student_grades(scored_responses, assessment_id=assessment_id)
    result = GradePushBatchResult(
        assessment_id=assessment_id,
        classroom_id=classroom_id,
        skipped_count=skipped,
    )

    # 1. Always write local ledger (authoritative record)
    ledger_data = {
        sid: {"score": info["score"], "questions": info["questions"]}
        for sid, info in aggregated.items()
    }
    ledger_path = _write_ledger(local_dir, classroom_id, assessment_id, ledger_data)
    result.ledger_path = str(ledger_path)
    result.local_logged_count = len(aggregated)

    # 2. LMS push (if provided)
    if lms is None or canvas_course_id is None or canvas_assignment_id is None:
        return result

    for student_id, info in aggregated.items():
        try:
            outcome = lms.push_grade(
                course_id=canvas_course_id,
                assignment_id=canvas_assignment_id,
                student_id=student_id,
                score=info["score"],
                comment="",
            )
            if getattr(outcome, "success", False):
                result.pushed_count += 1
            else:
                result.failures.append({
                    "student_id": student_id,
                    "message": getattr(outcome, "message", "unknown"),
                })
        except Exception as exc:
            result.failures.append({
                "student_id": student_id,
                "message": f"{type(exc).__name__}: {exc}",
            })

    return result


# ---------------------------------------------------------------------------
# push_grade_claims — from federation-transported signed claims (ADR-023)
# ---------------------------------------------------------------------------


def push_grade_claims(
    claims: Iterable[dict],
    assessment_id: str,
    classroom_id: str,
    canvas_course_id: str | None,
    canvas_assignment_id: str | None,
    lms: LMSGradePush | None,
    local_dir: Path,
    trust_verifier: Callable[[dict], bool],
) -> GradePushBatchResult:
    """Push grades that arrived as signed cross-node claims.

    Each claim is verified via `trust_verifier` (federation layer
    holds the trust chain). Rejected claims are logged but not
    aggregated or pushed. Accepted claims convert to ScoredResponse
    and flow through `push_grades()`.
    """
    accepted: list[ScoredResponse] = []
    rejected = 0

    for claim in claims:
        if not trust_verifier(claim):
            rejected += 1
            continue
        accepted.append(
            ScoredResponse(
                student_id=claim["student_id"],
                assessment_id=claim["assessment_id"],
                question_id=claim["question_id"],
                question_type=claim.get("question_type", "mcq"),
                final_score=claim.get("final_score"),
                needs_review=False,
                reviewed_by=claim.get("reviewed_by"),
            )
        )

    result = push_grades(
        scored_responses=accepted,
        assessment_id=assessment_id,
        classroom_id=classroom_id,
        canvas_course_id=canvas_course_id,
        canvas_assignment_id=canvas_assignment_id,
        lms=lms,
        local_dir=local_dir,
    )
    result.rejected_count = rejected
    return result


# ---------------------------------------------------------------------------
# Composition integration (#73)
# ---------------------------------------------------------------------------


def record_ledger_entry(
    composition: CompositionService,
    classroom_id: str,
    assessment_id: str,
    student_id: str,
    score: float,
    questions: int,
    instructor_id: str | None = None,
) -> MemoryFragment:
    """Materialize a per-student-per-assessment aggregate grade as
    MemoryFragment(semantic).

    Ownership: student = master; instructor gets CONTROL+GOALS delegation
    (can re-grade; set goals for the assessment) per ADR-026.

    Complements the local ledger JSONL (which stays authoritative for
    human-readable grade history) by bringing grade entries into the
    unified memory layer — they become accessible to RPE, signals,
    federation, and audit uniformly.
    """
    from axiom.memory.ownership import (
        Right,
        new_ownership,
    )
    from axiom.memory.ownership import (
        delegate as _delegate,
    )

    own = new_ownership(master=student_id)
    if instructor_id:
        own = _delegate(
            own,
            delegate_principal=instructor_id,
            rights={Right.CONTROL, Right.GOALS},
            expires_at="2099-12-31T23:59:59Z",
        )

    return composition.write(
        content={
            "student_id": student_id,
            "assessment_id": assessment_id,
            "classroom_id": classroom_id,
            "score": score,
            "questions": questions,
            "fact_kind": "grade_aggregate",
        },
        cognitive_type="semantic",
        principal_id=student_id,
        agents={"chalke"},
        resources={
            f"assessment:{assessment_id}",
            f"classroom:{classroom_id}",
            "grade-ledger",
        },
        ownership=own,
    )
