# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Classroom — a live instance of a Course for a specific term/cohort."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from axiom.artifacts import ArtifactRegistry
from axiom.classroom.course import CourseService


@dataclass
class Classroom:
    id: str
    course_id: str
    term: str
    instructor: str
    status: str
    roster: list[str] = field(default_factory=list)
    archived_at: float | None = None
    archive_reason: str | None = None


class ClassroomService:
    """Lifecycle + enrollment for live classroom instances."""

    KIND = "classroom"

    def __init__(self, *, registry: ArtifactRegistry, courses: CourseService) -> None:
        self._registry = registry
        self._courses = courses

    def open(self, *, course_id: str, term: str, instructor: str) -> str:
        # Confirm the course exists (raises KeyError → translate to LookupError).
        try:
            self._courses.get(course_id)
        except KeyError as e:
            raise LookupError(f"course {course_id!r} not found") from e

        return self._registry.register(
            kind=self.KIND,
            name=f"{course_id}:{term}",
            data={
                "course_id": course_id,
                "term": term,
                "instructor": instructor,
                "status": "open",
                "roster": [],
            },
        )

    def get(self, classroom_id: str) -> Classroom:
        a = self._registry.get(classroom_id)
        d = a.data
        return Classroom(
            id=a.id,
            course_id=d["course_id"],
            term=d["term"],
            instructor=d["instructor"],
            status=d["status"],
            roster=list(d.get("roster", [])),
            archived_at=d.get("archived_at"),
            archive_reason=d.get("archive_reason"),
        )

    VALID_ROLES = {"instructor", "ta", "student", "observer"}

    def enroll(
        self, classroom_id: str, *, student: str, role: str = "student"
    ) -> None:
        if role not in self.VALID_ROLES:
            raise ValueError(
                f"invalid role {role!r}; must be one of {sorted(self.VALID_ROLES)}"
            )
        a = self._registry.get(classroom_id)
        if a.data["status"] != "open":
            raise RuntimeError(f"classroom {classroom_id} is not open")
        if student not in a.data["roster"]:
            a.data["roster"].append(student)
        a.data.setdefault("roles", {})[student] = role

    def unenroll(self, classroom_id: str, *, student: str) -> None:
        a = self._registry.get(classroom_id)
        if student in a.data.get("roster", []):
            a.data["roster"].remove(student)
        a.data.get("roles", {}).pop(student, None)

    def roles(self, classroom_id: str) -> dict[str, str]:
        return dict(self._registry.get(classroom_id).data.get("roles", {}))

    def archive(self, classroom_id: str, *, reason: str | None = None) -> None:
        a = self._registry.get(classroom_id)
        a.data["status"] = "archived"
        a.data["archived_at"] = time.time()
        a.data["archive_reason"] = reason
