# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Conflict detection across time slots that share a scarce resource.

Two slots conflict when they carry the same ``resource_key`` and their planned
windows overlap. PULSE only uses ``resource_key`` for this — it is the one piece
of a slot PULSE compares; everything else stays opaque consumer metadata. A
``fixed`` slot is immovable: a conflict against it means "reschedule around it,
never onto it." ``priority`` orders preemption (higher wins).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Optional


class ConflictError(Exception):
    """Raised when a placement collides with a slot that cannot yield (a fixed
    slot, or higher priority). ``.conflicts`` carries the offending slots."""

    def __init__(self, conflicts: list[dict]) -> None:
        super().__init__(f"time-slot conflict with {len(conflicts)} slot(s)")
        self.conflicts = conflicts


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _overlaps(
    a_start: datetime, a_end: Optional[datetime],
    b_start: datetime, b_end: Optional[datetime],
) -> bool:
    a_start, a_end = _aware(a_start), _aware(a_end)
    b_start, b_end = _aware(b_start), _aware(b_end)
    ae = a_end or a_start  # a zero-width point if no end
    be = b_end or b_start
    if a_start == b_start:
        return True  # same reservation instant
    return a_start < be and b_start < ae


def find_conflicts(
    resource_key: Optional[str],
    planned_start: datetime,
    planned_end: Optional[datetime],
    *,
    exclude: Optional[str] = None,
) -> list[dict]:
    """Slots that conflict with the given window on ``resource_key``."""
    if not resource_key:
        return []
    from axiom.extensions.builtins.schedule import store
    from axiom.extensions.builtins.schedule.db_models import ScheduleTimeSlot

    with store.session_scope() as s:
        rows = (
            s.query(ScheduleTimeSlot)
            .filter(ScheduleTimeSlot.resource_key == resource_key)
            .filter(ScheduleTimeSlot.state != "cancelled")
            .all()
        )
        out = []
        for r in rows:
            if exclude is not None and r.id == exclude:
                continue
            if _overlaps(planned_start, planned_end, r.planned_start, r.planned_end):
                out.append({
                    "time_slot_id": r.id,
                    "planned_start": r.planned_start,
                    "planned_end": r.planned_end,
                    "fixed": r.fixed,
                    "priority": r.priority,
                })
        return out


__all__ = ["ConflictError", "find_conflicts"]
