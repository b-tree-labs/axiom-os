# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Classroom instance preparation — instantiating a course for a cohort.

A Classroom is the runtime instantiation: a specific cohort at a
specific time, with a specific RAG policy, an LMS connection, and
a dry-run verification. The reusable template lives in course_prep.

Spec: spec-classroom.md §2.6 (Course vs Classroom separation) +
§5.11 (classroom ↔ federation primitive mapping). Each classroom
is an ephemeral federation cohort: coordinator = instructor,
members = student nodes.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ClassroomPrepStep:
    """Single step in the classroom preparation checklist."""

    name: str
    description: str
    status: str = "pending"
    message: str = ""
    critical: bool = False


@dataclass
class ClassroomPrepChecklist:
    """Classroom instance preparation state."""

    instructor_id: str
    classroom_id: str
    course_id: str | None = None
    course_version: str | None = None
    steps: list[ClassroomPrepStep] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Checklist creation
# ---------------------------------------------------------------------------

_INSTANCE_STEPS = [
    ClassroomPrepStep(
        name="course_selected",
        description="Published course selected (new or existing)",
        critical=True,
    ),
    ClassroomPrepStep(
        name="rag_policy_selected",
        description="RAG routing policy chosen (course-only / institutional / A/B)",
        critical=True,
    ),
    ClassroomPrepStep(
        name="lms_connected",
        description="LMS (Canvas) connected and roster previewed",
        critical=True,
    ),
    ClassroomPrepStep(
        name="dry_run_passed",
        description="Instructor ran a dry-run as a test student",
        critical=False,
    ),
]


def create_classroom_prep_checklist(
    instructor_id: str, classroom_id: str
) -> ClassroomPrepChecklist:
    """Create a fresh classroom preparation checklist (4 instance steps)."""
    steps = [deepcopy(s) for s in _INSTANCE_STEPS]
    return ClassroomPrepChecklist(
        instructor_id=instructor_id,
        classroom_id=classroom_id,
        steps=steps,
    )


# ---------------------------------------------------------------------------
# Step validators
# ---------------------------------------------------------------------------


def validate_course_selected_step(
    checklist: ClassroomPrepChecklist,
    course_id: str,
    course_version: str,
    publishable: bool,
) -> ClassroomPrepChecklist:
    """Validate that a course has been selected and is publishable."""
    checklist = deepcopy(checklist)
    step = checklist.steps[0]

    if not course_id:
        step.status = "failed"
        step.message = "No course selected"
    elif not publishable:
        step.status = "failed"
        step.message = (
            f"Course '{course_id}' v{course_version} is not publishable — "
            "complete critical course prep steps first"
        )
    else:
        step.status = "completed"
        step.message = f"Course '{course_id}' v{course_version} selected"
        checklist.course_id = course_id
        checklist.course_version = course_version

    return checklist


def validate_rag_policy_step(
    checklist: ClassroomPrepChecklist,
    policy_id: str,
    policy_name: str,
) -> ClassroomPrepChecklist:
    """Validate RAG policy is selected."""
    checklist = deepcopy(checklist)
    step = checklist.steps[1]

    if not policy_id:
        step.status = "failed"
        step.message = "No RAG policy selected"
    else:
        step.status = "completed"
        step.message = f"RAG policy '{policy_name}' ({policy_id}) selected"

    return checklist


def validate_lms_step(
    checklist: ClassroomPrepChecklist,
    lms_connected: bool,
    roster_count: int,
) -> ClassroomPrepChecklist:
    """Validate LMS connection and roster preview."""
    checklist = deepcopy(checklist)
    step = checklist.steps[2]

    if not lms_connected:
        step.status = "failed"
        step.message = "LMS not connected — Canvas API token may be missing or invalid"
    elif roster_count == 0:
        step.status = "warning"
        step.message = "LMS connected but course roster is empty"
    else:
        step.status = "completed"
        step.message = f"LMS connected, {roster_count} students in roster"

    return checklist


def validate_dry_run_step(
    checklist: ClassroomPrepChecklist,
    dry_run_completed: bool,
) -> ClassroomPrepChecklist:
    """Validate instructor completed a dry-run."""
    checklist = deepcopy(checklist)
    step = checklist.steps[3]

    if dry_run_completed:
        step.status = "completed"
        step.message = "Dry run completed — instructor experienced the full student flow"
    else:
        step.status = "pending"
        step.message = "Dry run not yet completed (recommended before enrollment)"

    return checklist


# ---------------------------------------------------------------------------
# Enrollment readiness gate
# ---------------------------------------------------------------------------


def check_classroom_ready_for_enrollment(
    checklist: ClassroomPrepChecklist,
) -> tuple[bool, list[str]]:
    """Check if the classroom is ready to enroll students.

    Critical steps (course_selected, rag_policy, lms_connected) must
    be completed or in a warning state. Dry-run is recommended but
    does not block enrollment.
    """
    blockers = []
    for step in checklist.steps:
        if step.critical and step.status not in ("completed", "warning"):
            blockers.append(f"{step.name}: {step.description} ({step.status})")
    return len(blockers) == 0, blockers
