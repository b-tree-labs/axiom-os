# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LMS provider interface — abstract contract for Canvas, Moodle, Blackboard, etc.

Every LMS provider implements this interface. The classroom extension
talks to this interface, never to a specific LMS. Swapping LMS backends
is a config change, not a code change (ADR-012 provider pattern).
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field

from axiom.infra.provider_base import ProviderBase

# ---------------------------------------------------------------------------
# Data models — shared across all LMS implementations
# ---------------------------------------------------------------------------


@dataclass
class LMSStudent:
    """A student record from the LMS roster."""

    student_id: str
    name: str
    email: str
    role: str = "student"
    # Nationality is NOT from the LMS — it's an Axiom-side attestation.
    # Stays None until the instructor signs an attestation at enrollment.
    nationality: str | None = None


@dataclass
class GradePushResult:
    """Result of pushing a grade to the LMS."""

    success: bool
    message: str = ""
    canvas_submission_id: str | None = None


@dataclass
class AssignmentCreateResult:
    """Result of creating an assignment in the LMS."""

    success: bool
    message: str = ""
    canvas_assignment_id: str | None = None


@dataclass
class EnrollmentChanges:
    """Delta between known students and current LMS roster."""

    added: list[LMSStudent] = field(default_factory=list)
    dropped: list[LMSStudent] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Abstract LMS provider
# ---------------------------------------------------------------------------


class LMSProvider(ProviderBase):
    """Abstract base for LMS integrations.

    Subclasses must implement all abstract methods. The classroom
    extension calls these methods; it never knows which LMS is behind
    them.
    """

    _log_prefix = "lms"

    @abstractmethod
    def get_roster(self, course_id: str) -> list[LMSStudent]:
        """Fetch the current student roster for a course.

        Returns only students (not teachers, TAs, or observers).
        Nationality is always None — that's an Axiom-side concern.
        """

    @abstractmethod
    def push_grade(
        self,
        course_id: str,
        assignment_id: str,
        student_id: str,
        score: float,
        comment: str = "",
    ) -> GradePushResult:
        """Push a grade for one student on one assignment."""

    @abstractmethod
    def create_assignment(
        self,
        course_id: str,
        name: str,
        description: str = "",
        points_possible: float = 100,
        due_at: str = "",
    ) -> AssignmentCreateResult:
        """Create an assignment in the LMS from a Course manifest entry."""

    @abstractmethod
    def get_student(self, course_id: str, student_id: str) -> LMSStudent | None:
        """Fetch a single student's info. Returns None if not found."""

    @abstractmethod
    def sync_enrollment_changes(
        self,
        course_id: str,
        known_student_ids: list[str],
    ) -> EnrollmentChanges:
        """Compare the current LMS roster to known students.

        Returns which students were added and which were dropped
        since the last sync.
        """
