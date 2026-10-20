# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Quiz scoring queue + auto-score (WF-4).

Per spec-classroom.md §3.1 WF-4:
  Auto-scores objective questions, presents free-response scoring
  queue to instructor with LLM-suggested scores, computes aggregate
  statistics. Canvas grade push is a separate optional concern
  (see grade_push module).

Standalone-first: objective auto-score needs no backend; LLM
free-text grading is optional (queued with no suggestion if no
grader provided). Federation stretch: grades serialize as signed
claims for cross-node aggregation (ADR-023).

Composition integration (#72): `record_scored_response(composition,
scored)` materializes a ScoredResponse as a MemoryFragment(semantic)
— a durable, signed, audit-logged fact about student performance.
Student is the owner (master); instructor holds a CONTROL+GOALS
delegation so they can override scores and set assessment goals.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from axiom.memory.composition import CompositionService
    from axiom.memory.fragment import MemoryFragment


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ScoredResponse:
    """A student's response after auto-score + optional LLM suggestion."""

    student_id: str
    assessment_id: str
    question_id: str
    question_type: str  # mcq, yes_no, likert, free_text

    auto_score: float | None = None       # deterministic (MCQ, yes_no)
    suggested_score: float | None = None  # LLM-suggested for free-text
    final_score: float | None = None      # post-review or = auto_score
    needs_review: bool = False               # free-text waiting for instructor

    rationale: str | None = None          # LLM rationale (if any)
    reviewed_by: str | None = None        # instructor who overrode
    reviewed_at: str | None = None
    review_note: str | None = None


# ---------------------------------------------------------------------------
# LLM grader protocol
# ---------------------------------------------------------------------------


LLMGrader = Callable[..., dict]
"""Signature: (question, answer, rubric, **kw) -> {'score': float, 'rationale': str}"""


# ---------------------------------------------------------------------------
# Auto-score
# ---------------------------------------------------------------------------


def auto_score(
    response: dict,
    answer_key: dict[str, Any],
    llm_grader: LLMGrader | None = None,
) -> ScoredResponse:
    """Score a response based on its question_type.

    - mcq, yes_no: deterministic match against answer_key → auto_score.
    - likert: records the value; no scoring (ungradable aggregate).
    - free_text: if llm_grader + rubric provided, get suggested_score;
      always queue for instructor review.
    """
    qtype = response["question_type"]
    qid = response["question_id"]
    base = ScoredResponse(
        student_id=response["student_id"],
        assessment_id=response["assessment_id"],
        question_id=qid,
        question_type=qtype,
    )

    if qtype in ("mcq", "yes_no"):
        expected = answer_key.get(qid)
        if expected is None:
            base.auto_score = None
            base.needs_review = True  # no key — instructor must handle
            return base
        score = 1.0 if response.get("answer") == expected else 0.0
        base.auto_score = score
        base.final_score = score
        return base

    if qtype == "likert":
        # Recorded but not scored; excluded from stats
        return base

    if qtype == "free_text":
        base.needs_review = True
        rubric = response.get("rubric")
        if llm_grader is not None and rubric:
            result = llm_grader(
                question=response.get("question", ""),
                answer=response.get("answer", ""),
                rubric=rubric,
            )
            base.suggested_score = float(result["score"])
            base.rationale = result.get("rationale")
        return base

    # Unknown type — queue
    base.needs_review = True
    return base


# ---------------------------------------------------------------------------
# Scoring queue
# ---------------------------------------------------------------------------


def scoring_queue(responses: list[ScoredResponse]) -> list[ScoredResponse]:
    """Return responses still pending instructor review."""
    return [r for r in responses if r.needs_review]


# ---------------------------------------------------------------------------
# Instructor override
# ---------------------------------------------------------------------------


def override_score(
    scored: ScoredResponse,
    final: float,
    reviewer: str,
    note: str | None = None,
) -> ScoredResponse:
    """Apply an instructor's final score to a queued response.

    Preserves `suggested_score` for audit trail (instructor's final
    may differ from LLM suggestion).
    """
    result = deepcopy(scored)
    result.final_score = final
    result.needs_review = False
    result.reviewed_by = reviewer
    result.reviewed_at = datetime.now(UTC).isoformat()
    result.review_note = note
    return result


# ---------------------------------------------------------------------------
# Aggregate stats
# ---------------------------------------------------------------------------


def _is_graded(r: ScoredResponse) -> bool:
    return r.final_score is not None and not r.needs_review


def assessment_stats(
    responses: list[ScoredResponse],
    assessment_id: str,
) -> dict:
    """Compute per-assessment aggregate stats.

    Excludes: likert responses (ungradable), responses still in
    the review queue, responses from other assessments.
    """
    scores = [
        r.final_score for r in responses
        if r.assessment_id == assessment_id
        and _is_graded(r)
        and r.final_score is not None
    ]
    if not scores:
        return {"count": 0, "mean": None, "median": None, "min": None, "max": None}
    return {
        "count": len(scores),
        "mean": statistics.fmean(scores),
        "median": statistics.median(scores),
        "min": min(scores),
        "max": max(scores),
    }


def per_student_grades(
    responses: list[ScoredResponse],
    assessment_id: str,
) -> dict[str, dict]:
    """Aggregate per-student grade for one assessment (avg of graded questions)."""
    by_student: dict[str, list[float]] = {}
    for r in responses:
        if r.assessment_id != assessment_id:
            continue
        if not _is_graded(r) or r.final_score is None:
            continue
        by_student.setdefault(r.student_id, []).append(r.final_score)

    return {
        sid: {
            "score": statistics.fmean(scores),
            "questions": len(scores),
        }
        for sid, scores in by_student.items()
    }


# ---------------------------------------------------------------------------
# Federation serialization (ADR-023)
# ---------------------------------------------------------------------------


def serialize_grade_claim(scored: ScoredResponse, signer_node: str) -> dict:
    """Produce a signed-claim dict for cross-node grade transport.

    Signature slot is reserved; federation layer fills it in with the
    node's signing key when the grade crosses a trust boundary.
    """
    return {
        "student_id": scored.student_id,
        "classroom_id": None,  # filled in by caller if available
        "assessment_id": scored.assessment_id,
        "question_id": scored.question_id,
        "question_type": scored.question_type,
        "final_score": scored.final_score,
        "suggested_score": scored.suggested_score,
        "reviewed_by": scored.reviewed_by,
        "signer_node": signer_node,
        "issued_at": datetime.now(UTC).isoformat(),
        "signature": None,
    }


# ---------------------------------------------------------------------------
# Composition integration (#72)
# ---------------------------------------------------------------------------


def record_scored_response(
    composition: CompositionService,
    scored: ScoredResponse,
    classroom_id: str,
    instructor_id: str | None = None,
) -> MemoryFragment:
    """Materialize a ScoredResponse as MemoryFragment(semantic).

    - cognitive_type: semantic (durable fact about the student)
    - principal_id: student_id (student owns their record)
    - agents: {"chalke"} (grading performed by the TA agent)
    - resources: {assessment:<id>, classroom:<id>}
    - ownership: student master; instructor gets CONTROL+GOALS delegation
      (can override + re-grade; cannot reallocate resources or direct
      effort) per ADR-026.

    The fragment is signed + audit-logged by CompositionService; routes
    through policy + transform at write time. Returns the fragment for
    caller indexing.
    """
    from axiom.memory.ownership import (
        Right,
        new_ownership,
    )
    from axiom.memory.ownership import (
        delegate as _delegate,
    )

    own = new_ownership(master=scored.student_id)
    if instructor_id:
        own = _delegate(
            own,
            delegate_principal=instructor_id,
            rights={Right.CONTROL, Right.GOALS},
            expires_at="2099-12-31T23:59:59Z",  # effectively permanent for v1
        )

    return composition.write(
        content={
            "student_id": scored.student_id,
            "assessment_id": scored.assessment_id,
            "question_id": scored.question_id,
            "question_type": scored.question_type,
            "auto_score": scored.auto_score,
            "suggested_score": scored.suggested_score,
            "final_score": scored.final_score,
            "needs_review": scored.needs_review,
            "rationale": scored.rationale,
            "reviewed_by": scored.reviewed_by,
            "reviewed_at": scored.reviewed_at,
            "review_note": scored.review_note,
            "classroom_id": classroom_id,
        },
        cognitive_type="semantic",
        principal_id=scored.student_id,
        agents={"chalke"},
        resources={
            f"assessment:{scored.assessment_id}",
            f"classroom:{classroom_id}",
        },
        ownership=own,
    )
