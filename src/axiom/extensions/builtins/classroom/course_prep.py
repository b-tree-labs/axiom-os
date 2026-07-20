# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Course template preparation — authoring a reusable course artifact.

A Course is the reusable template: manifest, corpus, system prompt,
assessments, and onboarding rails. A Classroom is the runtime
instantiation (students, RAG policy, LMS roster, active period).

This module is the pure state machine for COURSE authoring. Classroom
instance preparation lives in `classroom_prep.py`.

Spec: spec-classroom.md §2.6 (Course vs Classroom separation) + §2.7
(Course Manifest schema).
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class PrepStep:
    """Single step in the course preparation checklist."""

    name: str
    description: str
    status: str = "pending"  # pending, completed, warning, failed
    message: str = ""
    critical: bool = False


@dataclass
class CoursePrepChecklist:
    """Course template preparation state."""

    instructor_id: str
    course_id: str
    steps: list[PrepStep] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Checklist creation
# ---------------------------------------------------------------------------

_PREP_STEPS = [
    PrepStep(
        name="manifest_loaded",
        description="Course manifest (YAML) loaded and validated",
        critical=True,
    ),
    PrepStep(
        name="corpus_indexed",
        description="Course corpus uploaded, indexed, and retrieval-tested",
        critical=True,
    ),
    PrepStep(
        name="system_prompt_set",
        description="Course system prompt set and test query approved",
        critical=True,
    ),
    PrepStep(
        name="assessments_defined",
        description="Assessments scheduled with rubrics",
        critical=False,
    ),
    PrepStep(
        name="onboarding_rails_configured",
        description="Student onboarding questionnaires configured",
        critical=False,
    ),
]


def create_prep_checklist(instructor_id: str, course_id: str) -> CoursePrepChecklist:
    """Create a fresh course preparation checklist (5 template steps)."""
    steps = [deepcopy(s) for s in _PREP_STEPS]
    return CoursePrepChecklist(
        instructor_id=instructor_id,
        course_id=course_id,
        steps=steps,
    )


# ---------------------------------------------------------------------------
# Step validators
# ---------------------------------------------------------------------------


def validate_manifest_step(
    checklist: CoursePrepChecklist,
    manifest: dict[str, Any],
) -> CoursePrepChecklist:
    """Validate that the course manifest has required fields."""
    checklist = deepcopy(checklist)
    step = checklist.steps[0]

    missing = [f for f in ("id", "title", "version") if f not in manifest]

    if missing:
        step.status = "failed"
        step.message = f"Missing required fields: {', '.join(missing)}"
    else:
        step.status = "completed"
        step.message = (
            f"Manifest '{manifest.get('title', '')}' v{manifest.get('version', '')} validated"
        )

    return checklist


def validate_corpus_step(
    checklist: CoursePrepChecklist,
    corpus_doc_count: int,
    test_query: str,
    test_results: list[dict],
) -> CoursePrepChecklist:
    """Validate corpus is indexed and retrieval works."""
    checklist = deepcopy(checklist)
    step = checklist.steps[1]

    if corpus_doc_count == 0:
        step.status = "warning"
        step.message = (
            "No documents in corpus — students will have no course material to retrieve from"
        )
    elif not test_results:
        step.status = "warning"
        step.message = f"{corpus_doc_count} documents indexed but test query returned no results"
    else:
        step.status = "completed"
        step.message = (
            f"{corpus_doc_count} documents indexed, test query returned {len(test_results)} results"
        )

    return checklist


def validate_prompt_step(
    checklist: CoursePrepChecklist,
    system_prompt: str,
    test_response: str,
    instructor_approved: bool,
) -> CoursePrepChecklist:
    """Validate system prompt by showing a test response for instructor approval."""
    checklist = deepcopy(checklist)
    step = checklist.steps[2]

    if not system_prompt:
        step.status = "failed"
        step.message = "No system prompt set"
    elif not instructor_approved:
        step.status = "pending"
        step.message = "System prompt set but test response not yet approved by instructor"
    else:
        step.status = "completed"
        step.message = "System prompt approved after test query review"

    return checklist


def validate_assessment_step(
    checklist: CoursePrepChecklist,
    assessment_count: int,
) -> CoursePrepChecklist:
    """Validate assessments are defined."""
    checklist = deepcopy(checklist)
    step = checklist.steps[3]

    if assessment_count == 0:
        step.status = "warning"
        step.message = "No assessments scheduled — can be added later"
    else:
        step.status = "completed"
        step.message = f"{assessment_count} assessments scheduled"

    return checklist


def validate_rails_step(
    checklist: CoursePrepChecklist,
    rail_count: int,
) -> CoursePrepChecklist:
    """Validate onboarding rails are configured."""
    checklist = deepcopy(checklist)
    step = checklist.steps[4]

    if rail_count == 0:
        step.status = "warning"
        step.message = "No onboarding rails configured — defaults will be used"
    else:
        step.status = "completed"
        step.message = f"{rail_count} onboarding rails configured"

    return checklist


# ---------------------------------------------------------------------------
# Publish readiness (course ready to be instantiated as a classroom)
# ---------------------------------------------------------------------------


def check_course_ready_to_publish(
    checklist: CoursePrepChecklist,
) -> tuple[bool, list[str]]:
    """A course is publishable when the 3 critical template steps are done.

    Assessments and rails are non-critical (defaults exist); they can
    be added later without blocking the course from being instantiated
    into classrooms.
    """
    blockers = []
    for step in checklist.steps:
        if step.critical and step.status not in ("completed", "warning"):
            blockers.append(f"{step.name}: {step.description} ({step.status})")
    return len(blockers) == 0, blockers
