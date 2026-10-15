# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Instructor course conclusion workflow — post-completion wrap-up.

Mirrors the prep workflow: a checklist of steps the instructor
completes after the course ends. Handles: analytics export,
knowledge promotion, course template update, Canvas grade
finalization, archive + harvest bundle distribution.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field


@dataclass
class ConclusionStep:
    """Single step in the conclusion checklist."""

    name: str
    description: str
    status: str = "pending"
    message: str = ""
    critical: bool = False


@dataclass
class CourseConclusionChecklist:
    """Instructor's course conclusion state."""

    instructor_id: str
    classroom_id: str
    steps: list[ConclusionStep] = field(default_factory=list)


_CONCLUSION_STEPS = [
    ConclusionStep(
        name="end_of_course_instruments",
        description="End-of-course evaluation + student self-reflection collected",
        critical=True,
    ),
    ConclusionStep(
        name="final_grades_synced",
        description="Final grades pushed to Canvas and verified",
        critical=True,
    ),
    ConclusionStep(
        name="analytics_exported",
        description="Cohort analytics exported for research (IRB-compliant)",
        critical=False,
    ),
    ConclusionStep(
        name="knowledge_promotion_reviewed",
        description="CURIO's promotion candidates reviewed and decided",
        critical=False,
    ),
    ConclusionStep(
        name="course_template_updated",
        description="Course template updated with this semester's improvements",
        critical=False,
    ),
    ConclusionStep(
        name="harvest_bundles_distributed",
        description="Student + instructor harvest bundles generated and distributed",
        critical=True,
    ),
    ConclusionStep(
        name="classroom_archived",
        description="Classroom transitioned to archived state (read-only)",
        critical=True,
    ),
    ConclusionStep(
        name="alumni_transition",
        description="Students' longitudinal identities established for future courses",
        critical=False,
    ),
]


def create_conclusion_checklist(instructor_id: str, classroom_id: str) -> CourseConclusionChecklist:
    """Create a fresh conclusion checklist."""
    return CourseConclusionChecklist(
        instructor_id=instructor_id,
        classroom_id=classroom_id,
        steps=[deepcopy(s) for s in _CONCLUSION_STEPS],
    )


def validate_grades_step(
    checklist: CourseConclusionChecklist,
    grades_synced: bool,
    student_count: int,
    grades_pushed: int,
) -> CourseConclusionChecklist:
    """Validate final grades are synced to Canvas."""
    checklist = deepcopy(checklist)
    step = checklist.steps[1]

    if not grades_synced:
        step.status = "failed"
        step.message = "Grade sync to Canvas failed"
    elif grades_pushed < student_count:
        step.status = "warning"
        step.message = f"{grades_pushed}/{student_count} grades pushed (some missing)"
    else:
        step.status = "completed"
        step.message = f"All {grades_pushed} grades synced to Canvas"

    return checklist


def validate_harvest_step(
    checklist: CourseConclusionChecklist,
    bundles_generated: int,
    student_count: int,
) -> CourseConclusionChecklist:
    """Validate harvest bundles generated for all participants."""
    checklist = deepcopy(checklist)
    step = checklist.steps[5]

    if bundles_generated == 0:
        step.status = "failed"
        step.message = "No harvest bundles generated"
    elif bundles_generated < student_count + 1:  # +1 for instructor bundle
        step.status = "warning"
        step.message = (
            f"{bundles_generated}/{student_count + 1} bundles generated (including instructor)"
        )
    else:
        step.status = "completed"
        step.message = f"All {bundles_generated} harvest bundles ready for distribution"

    return checklist


def validate_archive_step(
    checklist: CourseConclusionChecklist,
    archived: bool,
) -> CourseConclusionChecklist:
    """Validate classroom is archived."""
    checklist = deepcopy(checklist)
    step = checklist.steps[6]

    if archived:
        step.status = "completed"
        step.message = "Classroom archived (read-only mode)"
    else:
        step.status = "pending"
        step.message = "Classroom not yet archived"

    return checklist


def check_conclusion_completeness(
    checklist: CourseConclusionChecklist,
) -> tuple[bool, list[str]]:
    """Check if critical conclusion steps are complete."""
    blockers = []
    for step in checklist.steps:
        if step.critical and step.status not in ("completed", "warning"):
            blockers.append(f"{step.name}: {step.description} ({step.status})")
    return len(blockers) == 0, blockers
