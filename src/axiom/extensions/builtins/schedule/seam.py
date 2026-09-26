# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""The scheduling consumer seam.

A small, domain-agnostic contract a consumer extension's CLI verb wraps
(per ADR-056) to reserve time and react to it, without re-implementing
scheduling. Axiom owns the generic primitive; the consumer maps its domain
into the opaque ``metadata`` dict, which PULSE stores and returns verbatim and
never interprets.

The four calls:

- ``register_time_slot(planned_start, planned_end, metadata) -> time_slot_id``
- ``record_actual(time_slot_id, actual_start, actual_end)``
- ``register_cadence(cadence, action, time_slot_id=...) -> schedule_id``  (rides PULSE)
- ``time_slot_status(time_slot_id) -> dict``

A consumer composes these: reserve a slot for a planned time, register a
cadence (a reminder / countdown timer) against it, let PULSE fire the cadence,
and record the actual time when the event happens. The planned-vs-actual gap is
the consumer's signal — Axiom just keeps the record.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Optional

from axiom.extensions.builtins.schedule import store
from axiom.extensions.builtins.schedule.api import Cadence, ScheduleId, register
from axiom.extensions.builtins.schedule.db_models import ScheduleTimeSlot


class AllocationError(Exception):
    """Raised when the register-time allocation gate vetoes a reservation."""


def register_time_slot(
    *,
    planned_start: datetime,
    planned_end: Optional[datetime] = None,
    metadata: Optional[dict] = None,
    resource_key: Optional[str] = None,
    fixed: bool = False,
    priority: int = 0,
    reject_on_conflict: bool = False,
    now: Optional[datetime] = None,
) -> str:
    """Reserve a time window. ``metadata`` is opaque and round-tripped verbatim.

    If ``resource_key`` is set, overlapping reservations on the same key are
    detected: ``on_conflict`` surfaces via the ``on_conflict`` hook, and
    ``reject_on_conflict=True`` raises ``ConflictError`` instead of reserving.
    ``fixed`` marks the slot immovable; ``priority`` orders preemption.
    """
    from axiom.extensions.builtins.schedule import hooks
    from axiom.extensions.builtins.schedule.conflicts import (
        ConflictError,
        find_conflicts,
    )

    # Allocation gate (register-time, distinct from fire-time authz): a
    # registered pre_register check can veto the reservation (no quota / no
    # allocation). Fail-closed, like pre_fire.
    allowed, deny_reason = hooks.gate(
        hooks.PRE_REGISTER,
        {"planned_start": planned_start, "resource_key": resource_key, "metadata": metadata},
    )
    if not allowed:
        raise AllocationError(deny_reason or "allocation denied")

    time_slot_id = uuid.uuid4().hex
    existing = find_conflicts(resource_key, planned_start, planned_end)
    if existing and reject_on_conflict:
        raise ConflictError(existing)

    row = ScheduleTimeSlot(
        id=time_slot_id,
        planned_start=planned_start,
        planned_end=planned_end,
        time_slot_metadata=metadata,
        resource_key=resource_key,
        fixed=fixed,
        priority=priority,
        state="reserved",
        created_at=now or datetime.now(UTC),
    )
    with store.session_scope() as s:
        s.add(row)
        s.commit()
    if existing:
        hooks.emit(
            hooks.ON_CONFLICT,
            {"time_slot_id": time_slot_id, "resource_key": resource_key,
             "conflicts": existing},
        )
    return time_slot_id


def record_actual(
    time_slot_id: str,
    *,
    actual_start: datetime,
    actual_end: Optional[datetime] = None,
) -> None:
    """Record what actually happened against a reserved slot."""
    with store.session_scope() as s:
        row = s.get(ScheduleTimeSlot, time_slot_id)
        if row is None:
            raise KeyError(f"no such slot: {time_slot_id}")
        row.actual_start = actual_start
        if actual_end is not None:
            row.actual_end = actual_end
        row.state = "done" if actual_end is not None else "active"
        s.commit()
    from axiom.extensions.builtins.schedule import anchor, hooks

    # The anchor: recording actual is what a consumer hangs dependent timers
    # off of (count window opens N hours after actual_end, etc.).
    hooks.emit(
        hooks.ON_ACTUAL_RECORDED,
        {
            "time_slot_id": time_slot_id,
            "actual_start": actual_start,
            "actual_end": actual_end,
        },
    )
    anchor.recompute_dependents(
        time_slot_id, actual_start=actual_start, actual_end=actual_end
    )


def register_cadence(
    *,
    cadence: Cadence,
    action: str,
    time_slot_id: Optional[str] = None,
    anchor_to: Optional[str] = None,
    anchor_offset: Optional[Any] = None,
    envelope: Any = None,
    description: str = "",
    retry_policy: Optional[dict] = None,
    now: Optional[datetime] = None,
) -> ScheduleId:
    """Register a cadence on PULSE; optionally bind it to a slot.

    Two binding modes:
    - **Planned-relative** (``time_slot_id`` only): the slot's primary cadence —
      a reminder/timer that moves with the slot's planned time (see
      ``reschedule_time_slot``).
    - **Actual-anchored** (``time_slot_id`` + ``anchor_to`` ``actual_start`` /
      ``actual_end`` + ``anchor_offset`` timedelta): dormant until the actual is
      recorded, then fires at ``actual_<anchor_to> + offset`` (the anchor).
    """
    anchored = anchor_to is not None and time_slot_id is not None
    schedule_id = register(
        envelope=envelope,
        cadence=cadence,
        action=action,
        description=description,
        retry_policy=retry_policy,
        now=now,
        anchor_time_slot_id=time_slot_id if anchored else None,
        anchor_to=anchor_to if anchored else None,
        anchor_offset_seconds=(
            int(anchor_offset.total_seconds())
            if anchored and anchor_offset is not None
            else 0
        ),
    )
    if time_slot_id is not None and not anchored:
        with store.session_scope() as s:
            row = s.get(ScheduleTimeSlot, time_slot_id)
            if row is not None:
                row.schedule_id = str(schedule_id)
                s.commit()
    return schedule_id


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def reschedule_time_slot(
    time_slot_id: str,
    *,
    new_planned_start: datetime,
    new_planned_end: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Move a reserved time window. A linked cadence (reminder/timer) **follows
    by the same delta**, preserving its relative offset to the slot — so the
    operator moving an experiment carries its reminder with it.
    """
    from axiom.extensions.builtins.schedule import hooks
    from axiom.extensions.builtins.schedule.api import reschedule, status

    new_planned_start = _aware(new_planned_start)
    new_planned_end = _aware(new_planned_end)
    with store.session_scope() as s:
        row = s.get(ScheduleTimeSlot, time_slot_id)
        if row is None:
            raise KeyError(f"no such time_slot: {time_slot_id}")
        old_start = _aware(row.planned_start)
        row.planned_start = new_planned_start
        if new_planned_end is not None:
            row.planned_end = new_planned_end
        linked = row.schedule_id
        s.commit()

    delta = new_planned_start - old_start
    if linked:
        current = status(linked).summary.next_fire_at
        if current is not None:
            reschedule(linked, next_fire_at=_aware(current) + delta, now=now)

    hooks.emit(
        hooks.ON_RESCHEDULE,
        {
            "time_slot_id": time_slot_id,
            "old_planned_start": old_start,
            "new_planned_start": new_planned_start,
            "linked_schedule_id": linked,
            "delta_seconds": delta.total_seconds(),
        },
    )
    return time_slot_status(time_slot_id)


def time_slot_status(time_slot_id: str) -> dict:
    """Read back a slot: planned, actual, metadata, state, linked cadence."""
    with store.session_scope() as s:
        row = s.get(ScheduleTimeSlot, time_slot_id)
        if row is None:
            raise KeyError(f"no such slot: {time_slot_id}")
        return {
            "time_slot_id": row.id,
            "planned_start": row.planned_start,
            "planned_end": row.planned_end,
            "actual_start": row.actual_start,
            "actual_end": row.actual_end,
            "metadata": row.time_slot_metadata,
            "schedule_id": row.schedule_id,
            "resource_key": row.resource_key,
            "fixed": row.fixed,
            "priority": row.priority,
            "proposed_planned_start": row.proposed_planned_start,
            "state": row.state,
        }


def propose_reschedule(
    time_slot_id: str,
    *,
    new_planned_start: datetime,
    new_planned_end: Optional[datetime] = None,
) -> dict:
    """Record a **pending** reschedule for operator confirmation (operator-veto).

    Surfaces any conflicts at the proposed time but does NOT apply the move —
    a requester proposes; an operator confirms. Returns the proposal + conflicts.
    """
    from axiom.extensions.builtins.schedule import hooks
    from axiom.extensions.builtins.schedule.conflicts import find_conflicts

    with store.session_scope() as s:
        row = s.get(ScheduleTimeSlot, time_slot_id)
        if row is None:
            raise KeyError(f"no such time_slot: {time_slot_id}")
        row.proposed_planned_start = new_planned_start
        row.proposed_planned_end = new_planned_end
        resource_key = row.resource_key
        s.commit()

    conflicts = find_conflicts(
        resource_key, new_planned_start, new_planned_end, exclude=time_slot_id
    )
    if conflicts:
        hooks.emit(
            hooks.ON_CONFLICT,
            {"time_slot_id": time_slot_id, "phase": "proposed", "conflicts": conflicts},
        )
    return {
        "time_slot_id": time_slot_id,
        "proposed_planned_start": new_planned_start,
        "conflicts": conflicts,
    }


def confirm_reschedule(time_slot_id: str, *, now: Optional[datetime] = None) -> dict:
    """Apply a pending proposal (operator confirms). Refuses if the proposed
    time collides with a **fixed** slot — you reschedule around an immovable
    slot, never onto it."""
    from axiom.extensions.builtins.schedule.conflicts import (
        ConflictError,
        find_conflicts,
    )

    with store.session_scope() as s:
        row = s.get(ScheduleTimeSlot, time_slot_id)
        if row is None:
            raise KeyError(f"no such time_slot: {time_slot_id}")
        proposed_start = row.proposed_planned_start
        if proposed_start is None:
            raise ValueError(f"no pending reschedule proposal for {time_slot_id}")
        proposed_end = row.proposed_planned_end
        resource_key = row.resource_key

    blocking = [
        c
        for c in find_conflicts(
            resource_key, proposed_start, proposed_end, exclude=time_slot_id
        )
        if c["fixed"]
    ]
    if blocking:
        raise ConflictError(blocking)

    result = reschedule_time_slot(
        time_slot_id,
        new_planned_start=proposed_start,
        new_planned_end=proposed_end,
        now=now,
    )
    with store.session_scope() as s:
        row = s.get(ScheduleTimeSlot, time_slot_id)
        if row is not None:
            row.proposed_planned_start = None
            row.proposed_planned_end = None
            s.commit()
    return result


def reject_reschedule(time_slot_id: str) -> dict:
    """Discard a pending reschedule proposal (operator declines)."""
    with store.session_scope() as s:
        row = s.get(ScheduleTimeSlot, time_slot_id)
        if row is None:
            raise KeyError(f"no such time_slot: {time_slot_id}")
        row.proposed_planned_start = None
        row.proposed_planned_end = None
        s.commit()
    return {"time_slot_id": time_slot_id, "rejected": True}


def cancel_time_slot(time_slot_id: str, *, now: Optional[datetime] = None) -> dict:
    """Cancel a reserved time-slot and **cascade-cancel its cadences** — the
    planned-relative reminder and every actual-anchored timer bound to it."""
    from axiom.extensions.builtins.schedule import hooks
    from axiom.extensions.builtins.schedule.api import cancel
    from axiom.extensions.builtins.schedule.db_models import ScheduleDefinition

    with store.session_scope() as s:
        row = s.get(ScheduleTimeSlot, time_slot_id)
        if row is None:
            raise KeyError(f"no such time_slot: {time_slot_id}")
        row.state = "cancelled"
        linked = row.schedule_id
        anchored = [
            r.id
            for r in s.query(ScheduleDefinition)
            .filter(ScheduleDefinition.anchor_time_slot_id == time_slot_id)
            .filter(ScheduleDefinition.state == "active")
            .all()
        ]
        s.commit()

    cascaded: list[str] = []
    for sid in ([linked] if linked else []) + anchored:
        cancel(sid, now=now)
        cascaded.append(str(sid))
    hooks.emit(
        hooks.ON_CANCEL,
        {"time_slot_id": time_slot_id, "cascaded_cadences": cascaded},
    )
    return {"time_slot_id": time_slot_id, "state": "cancelled", "cascaded_cadences": cascaded}


__all__ = [
    "AllocationError",
    "cancel_time_slot",
    "confirm_reschedule",
    "propose_reschedule",
    "register_time_slot",
    "record_actual",
    "register_cadence",
    "reject_reschedule",
    "reschedule_time_slot",
    "time_slot_status",
]
