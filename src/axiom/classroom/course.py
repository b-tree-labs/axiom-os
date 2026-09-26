# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Course — a reusable template. Not a live session; that's Classroom."""

from __future__ import annotations

from dataclasses import dataclass

from axiom.artifacts import ArtifactRegistry


@dataclass
class Course:
    id: str
    name: str
    description: str
    owner: str
    deleted: bool = False
    deletion_reason: str | None = None


class CourseService:
    """Thin domain wrapper over ArtifactRegistry for course artifacts."""

    KIND = "course"

    def __init__(self, *, registry: ArtifactRegistry) -> None:
        self._registry = registry

    def create(self, *, name: str, owner: str, description: str = "") -> str:
        return self._registry.register(
            kind=self.KIND,
            name=name,
            data={"description": description, "owner": owner},
        )

    def get(self, course_id: str) -> Course:
        a = self._registry.get(course_id)
        return Course(
            id=a.id,
            name=a.name,
            description=a.data.get("description", ""),
            owner=a.data["owner"],
            deleted=a.deleted,
            deletion_reason=a.deletion_reason,
        )

    def list(self, *, include_deleted: bool = False) -> list[Course]:
        return [
            self.get(a.id)
            for a in self._registry.list(kind=self.KIND, include_deleted=include_deleted)
        ]

    def delete(self, course_id: str, *, reason: str | None = None) -> None:
        self._registry.delete(course_id, reason=reason)
