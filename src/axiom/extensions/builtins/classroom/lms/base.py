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


@dataclass
class LMSModule:
    """A module/section in an LMS course."""

    module_id: str
    course_id: str
    name: str
    position: int = 0

    @property
    def id(self) -> str:  # pragma: no cover - alias
        return self.module_id


@dataclass
class LMSModuleItem:
    """A single item within a module (page reference, file, assignment)."""

    item_id: str
    module_id: str
    course_id: str
    type: str               # "Page" | "File" | "Assignment" | "Quiz" | "ExternalUrl" ...
    title: str
    content_id: str = ""
    position: int = 0

    @property
    def id(self) -> str:  # pragma: no cover - alias
        return self.item_id


@dataclass
class LMSFile:
    """A file uploaded to a course."""

    file_id: str
    course_id: str
    display_name: str
    content_type: str = ""
    size: int = 0

    @property
    def id(self) -> str:  # pragma: no cover - alias
        return self.file_id


@dataclass
class LMSPage:
    """A wiki page in a course."""

    url_slug: str
    course_id: str
    title: str
    body: str = ""

    # Back-compat alias for older callers that use `page_url`.
    @property
    def page_url(self) -> str:  # pragma: no cover - trivial
        return self.url_slug


@dataclass
class LMSAnnouncement:
    """An announcement posted to a course."""

    announcement_id: str
    course_id: str
    title: str
    message: str = ""
    posted_at: str = ""
    author: str = ""

    @property
    def id(self) -> str:  # pragma: no cover - alias
        return self.announcement_id


@dataclass
class LMSWriteResult:
    """Result of a write operation against the LMS (page create, etc.)."""

    success: bool
    message: str = ""
    lms_id: str = ""
    url_slug: str = ""


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
