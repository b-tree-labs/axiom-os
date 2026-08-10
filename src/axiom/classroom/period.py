# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Period — a scheduled class meeting inside a Classroom.

Periods are the scope unit for transient RACI grants, presence tracking,
and natural-language policy broadcasts (per project_nl_policy_broadcasting).
When a Period ends, everything scoped to it expires automatically.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from axiom.artifacts import ArtifactRegistry
from axiom.classroom.classroom import ClassroomService


@dataclass
class Period:
    id: str
    classroom_id: str
    title: str
    starts_at: float
    ends_at: float
    status: str  # scheduled | in_progress | ended | cancelled
    actual_starts_at: float | None = None
    actual_ends_at: float | None = None
    present: list[str] = field(default_factory=list)


class PeriodService:
    """Lifecycle + presence for scheduled class meetings."""

    KIND = "period"
    VALID_STATUSES = {"scheduled", "in_progress", "ended", "cancelled"}

    def __init__(
        self, *, registry: ArtifactRegistry, classrooms: ClassroomService
    ) -> None:
        self._registry = registry
        self._classrooms = classrooms

    def schedule(
        self,
        *,
        classroom_id: str,
        title: str,
        starts_at: float,
        ends_at: float,
    ) -> str:
        # Confirm classroom exists.
        self._classrooms.get(classroom_id)
        return self._registry.register(
            kind=self.KIND,
            name=f"{classroom_id}:{title}",
            data={
                "classroom_id": classroom_id,
                "title": title,
                "starts_at": starts_at,
                "ends_at": ends_at,
                "status": "scheduled",
                "present": [],
            },
        )

    def get(self, period_id: str) -> Period:
        a = self._registry.get(period_id)
        d = a.data
        return Period(
            id=a.id,
            classroom_id=d["classroom_id"],
            title=d["title"],
            starts_at=d["starts_at"],
            ends_at=d["ends_at"],
            status=d["status"],
            actual_starts_at=d.get("actual_starts_at"),
            actual_ends_at=d.get("actual_ends_at"),
            present=list(d.get("present", [])),
        )

    def list_for_classroom(self, classroom_id: str) -> list[Period]:
        return [
            self.get(a.id)
            for a in self._registry.list(kind=self.KIND)
            if a.data.get("classroom_id") == classroom_id
        ]

    def start(self, period_id: str, *, now: float) -> None:
        a = self._registry.get(period_id)
        a.data["status"] = "in_progress"
        a.data["actual_starts_at"] = now

    def end(self, period_id: str, *, now: float) -> None:
        a = self._registry.get(period_id)
        a.data["status"] = "ended"
        a.data["actual_ends_at"] = now
        # Clear presence on end so period-scoped policies evaporate naturally.
        a.data["present"] = []

    def join(self, period_id: str, *, participant: str, now: float) -> None:
        a = self._registry.get(period_id)
        if a.data["status"] != "in_progress":
            raise RuntimeError(f"period {period_id} is not in progress")
        if participant not in a.data["present"]:
            a.data["present"].append(participant)

    def leave(self, period_id: str, *, participant: str, now: float) -> None:
        a = self._registry.get(period_id)
        if participant in a.data.get("present", []):
            a.data["present"].remove(participant)

    def present(self, period_id: str) -> list[str]:
        return list(self._registry.get(period_id).data.get("present", []))
