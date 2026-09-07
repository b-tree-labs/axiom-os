# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""PULSE public API — register / pause / resume / cancel / list / fire_now / status.

Per spec-axiom-schedule §3: every cadence kind collapses to a unified
``next_fire_at`` column on the schedule row. The API surface is the
same; only ``Cadence.kind`` differs.

PULSE-1 supports ``kind in {"one_shot", "interval", "cron"}``. The
``"trigger"`` kind is accepted by the dataclass (so PULSE-2 doesn't
need to break the shape) but raises ``NotImplementedError`` at
register-time.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Iterator, Literal, NewType, Optional


ScheduleId = NewType("ScheduleId", str)


@dataclass(frozen=True)
class Cadence:
    """The cadence shape per PRD §5.1.

    PULSE-1: ``one_shot`` | ``interval`` | ``cron`` | ``rrule`` (iCalendar
    RFC 5545 recurrence — the calendar lingua franca). ``trigger`` is accepted
    by the dataclass but raises at register-time.
    """

    kind: Literal["one_shot", "interval", "cron", "rrule", "trigger"]
    interval: Optional[timedelta] = None
    cron: Optional[str] = None
    rrule: Optional[str] = None
    tz: str = "UTC"
    not_before: Optional[datetime] = None
    not_after: Optional[datetime] = None
    randomized_delay: Optional[timedelta] = None


@dataclass(frozen=True)
class ScheduleReceipt:
    """The receipt shape returned by mutating verbs."""

    schedule_id: ScheduleId
    action_taken: str
    at: datetime


@dataclass(frozen=True)
class ScheduleSummary:
    """One row in ``axi schedule list`` output."""

    id: ScheduleId
    name: str
    state: str  # active | paused | cancelled
    cadence_kind: str
    next_fire_at: Optional[datetime]
    description: str


@dataclass(frozen=True)
class ScheduleStatus:
    """Detailed status for ``axi schedule status <id>``."""

    summary: ScheduleSummary
    last_fire_at: Optional[datetime]
    last_outcome: Optional[str]
    attempts_in_current_window: int
    dead_letter_count: int


def _cadence_payload(cadence: Cadence) -> dict:
    if cadence.kind == "interval":
        if cadence.interval is None:
            raise ValueError("interval cadence requires interval=...")
        return {"interval_seconds": int(cadence.interval.total_seconds())}
    if cadence.kind == "cron":
        if cadence.cron is None:
            raise ValueError("cron cadence requires cron=...")
        return {"cron_expr": cadence.cron, "tz": cadence.tz}
    if cadence.kind == "rrule":
        if cadence.rrule is None:
            raise ValueError("rrule cadence requires rrule=...")
        return {
            "rrule": cadence.rrule,
            "dtstart": cadence.not_before.isoformat() if cadence.not_before else None,
            "tz": cadence.tz,
        }
    return {}  # one_shot


def _envelope_dict(envelope: Any) -> Optional[dict]:
    if envelope is None:
        return None
    if isinstance(envelope, dict):
        return envelope
    to_dict = getattr(envelope, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return {"repr": repr(envelope)}


def cadence_from_payload(kind: str, payload: dict, *, tz: str = "UTC") -> Cadence:
    """Reconstruct a Cadence from the stored row — used by the engine to
    advance ``next_fire_at`` after a fire."""
    if kind == "interval":
        return Cadence(kind="interval", interval=timedelta(seconds=payload["interval_seconds"]))
    if kind == "cron":
        return Cadence(kind="cron", cron=payload["cron_expr"], tz=payload.get("tz", tz))
    if kind == "rrule":
        ds = payload.get("dtstart")
        return Cadence(
            kind="rrule",
            rrule=payload["rrule"],
            not_before=datetime.fromisoformat(ds) if ds else None,
            tz=payload.get("tz", tz),
        )
    return Cadence(kind="one_shot")


def register(
    *,
    envelope: Any,  # ActionEnvelope — typed loosely to avoid governance import cycle
    cadence: Cadence,
    action: str,
    description: str = "",
    extension: Optional[str] = None,
    retry_policy: Optional[dict] = None,
    classification_ceiling: Optional[str] = None,
    raci_default: str = "autonomous",
    misfire_policy: str = "fire_once",
    reentrant: bool = False,
    anchor_time_slot_id: Optional[str] = None,
    anchor_to: Optional[str] = None,
    anchor_offset_seconds: int = 0,
    compliance_window_seconds: Optional[int] = None,
    compliance_action: str = "flag",
    now: Optional[datetime] = None,
) -> ScheduleId:
    """Register a recurring or one-shot action; returns its id.

    Writes a ``schedule_definition`` row with the initial ``next_fire_at``
    computed from the cadence. Backed by ``session_for('schedule')`` (or the
    test-injected provider).
    """
    if cadence.kind == "trigger":
        raise NotImplementedError(
            "Trigger-style schedules ship in PULSE-2; see spec-axiom-schedule §7."
        )
    from axiom.extensions.builtins.schedule import store
    from axiom.extensions.builtins.schedule.cadence import compute_next_fire_at
    from axiom.extensions.builtins.schedule.db_models import ScheduleDefinition

    now = now or datetime.now(UTC)
    sid = uuid.uuid4().hex
    # An anchored cadence stays dormant (NULL next_fire_at) until the slot's
    # actual time is recorded and recompute_dependents sets its fire time.
    initial_next = (
        None if anchor_time_slot_id is not None
        else compute_next_fire_at(cadence, None, now)
    )
    row = ScheduleDefinition(
        id=sid,
        name=description or action,
        description=description,
        extension=extension,
        action=action,
        cadence_kind=cadence.kind,
        cadence_payload=_cadence_payload(cadence),
        next_fire_at=initial_next,
        anchor_time_slot_id=anchor_time_slot_id,
        anchor_to=anchor_to,
        anchor_offset_seconds=anchor_offset_seconds,
        compliance_window_seconds=compliance_window_seconds,
        compliance_action=compliance_action,
        not_before=cadence.not_before,
        not_after=cadence.not_after,
        randomized_delay_seconds=(
            int(cadence.randomized_delay.total_seconds())
            if cadence.randomized_delay
            else 0
        ),
        classification_ceiling=classification_ceiling,
        raci_default=raci_default,
        retry_policy=retry_policy or {"max_attempts": 1},
        misfire_policy=misfire_policy,
        reentrant=reentrant,
        capability_envelope=_envelope_dict(envelope),
        state="active",
    )
    with store.session_scope() as s:
        s.add(row)
        s.commit()
    from axiom.extensions.builtins.schedule import hooks

    hooks.emit(
        hooks.ON_REGISTER,
        {"schedule_id": sid, "action": action, "cadence_kind": cadence.kind},
    )
    return ScheduleId(sid)


def _set_state(schedule_id: ScheduleId, state: str, *, reason: str | None = None,
               now: Optional[datetime] = None) -> ScheduleReceipt:
    from axiom.extensions.builtins.schedule import hooks, store
    from axiom.extensions.builtins.schedule.db_models import ScheduleDefinition

    at = now or datetime.now(UTC)
    with store.session_scope() as s:
        row = s.get(ScheduleDefinition, str(schedule_id))
        if row is None:
            raise KeyError(f"no such schedule: {schedule_id}")
        row.state = state
        if reason is not None:
            row.paused_reason = reason
        s.commit()
    if state == "cancelled":
        hooks.emit(hooks.ON_CANCEL, {"schedule_id": str(schedule_id)})
    return ScheduleReceipt(schedule_id=schedule_id, action_taken=state, at=at)


def reschedule(
    schedule_id: ScheduleId,
    *,
    cadence: Optional[Cadence] = None,
    next_fire_at: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> ScheduleReceipt:
    """Move a schedule in time **without losing its id/history/fire-log**.

    Provide a new ``cadence`` (re-times the whole series) or an explicit
    ``next_fire_at`` (a single move). Emits ``ON_RESCHEDULE`` so consumers
    (e.g. a calendar connector) follow.
    """
    from axiom.extensions.builtins.schedule import hooks, store
    from axiom.extensions.builtins.schedule.cadence import compute_next_fire_at
    from axiom.extensions.builtins.schedule.db_models import ScheduleDefinition

    at = now or datetime.now(UTC)
    if cadence is not None and cadence.kind == "trigger":
        raise NotImplementedError("Trigger-style schedules ship in PULSE-2.")
    with store.session_scope() as s:
        row = s.get(ScheduleDefinition, str(schedule_id))
        if row is None:
            raise KeyError(f"no such schedule: {schedule_id}")
        old = row.next_fire_at
        if cadence is not None:
            row.cadence_kind = cadence.kind
            row.cadence_payload = _cadence_payload(cadence)
            row.next_fire_at = compute_next_fire_at(cadence, None, at)
        elif next_fire_at is not None:
            row.next_fire_at = next_fire_at
        else:
            raise ValueError("reschedule requires cadence= or next_fire_at=")
        new = row.next_fire_at
        s.commit()
    hooks.emit(
        hooks.ON_RESCHEDULE,
        {"schedule_id": str(schedule_id), "old_next_fire_at": old, "new_next_fire_at": new},
    )
    return ScheduleReceipt(schedule_id=schedule_id, action_taken="reschedule", at=at)


def pause(schedule_id: ScheduleId, reason: str, *, now: Optional[datetime] = None) -> ScheduleReceipt:
    """Pause a schedule. Engine skips paused rows in tick()."""
    return _set_state(schedule_id, "paused", reason=reason, now=now)


def resume(schedule_id: ScheduleId, *, now: Optional[datetime] = None) -> ScheduleReceipt:
    """Resume a paused schedule."""
    return _set_state(schedule_id, "active", now=now)


def cancel(schedule_id: ScheduleId, *, now: Optional[datetime] = None) -> ScheduleReceipt:
    """Cancel a schedule. Terminal state; not resumable."""
    return _set_state(schedule_id, "cancelled", now=now)


def list_schedules(
    state_filter: Optional[str] = None,
) -> Iterator[ScheduleSummary]:
    """Iterate registered schedules."""
    from axiom.extensions.builtins.schedule import store
    from axiom.extensions.builtins.schedule.db_models import ScheduleDefinition

    with store.session_scope() as s:
        q = s.query(ScheduleDefinition)
        if state_filter:
            q = q.filter(ScheduleDefinition.state == state_filter)
        for row in q.order_by(ScheduleDefinition.created_at).all():
            yield ScheduleSummary(
                id=ScheduleId(row.id),
                name=row.name,
                state=row.state,
                cadence_kind=row.cadence_kind,
                next_fire_at=row.next_fire_at,
                description=row.description,
            )


def status(schedule_id: ScheduleId) -> ScheduleStatus:
    """Detailed status for one schedule, including fire-log aggregates."""
    from axiom.extensions.builtins.schedule import store
    from axiom.extensions.builtins.schedule.db_models import (
        ScheduleDefinition,
        ScheduleFireLog,
    )

    with store.session_scope() as s:
        row = s.get(ScheduleDefinition, str(schedule_id))
        if row is None:
            raise KeyError(f"no such schedule: {schedule_id}")
        fires = (
            s.query(ScheduleFireLog)
            .filter(ScheduleFireLog.schedule_id == str(schedule_id))
            .order_by(ScheduleFireLog.intended_fire_at.desc())
            .all()
        )
        last = fires[0] if fires else None
        dead = sum(1 for f in fires if f.outcome == "dead_letter")
        summary = ScheduleSummary(
            id=ScheduleId(row.id),
            name=row.name,
            state=row.state,
            cadence_kind=row.cadence_kind,
            next_fire_at=row.next_fire_at,
            description=row.description,
        )
        return ScheduleStatus(
            summary=summary,
            last_fire_at=last.intended_fire_at if last else None,
            last_outcome=last.outcome if last else None,
            attempts_in_current_window=last.attempt if last else 0,
            dead_letter_count=dead,
        )


def fire_now(schedule_id: ScheduleId, *, now: Optional[datetime] = None) -> ScheduleReceipt:
    """Manual fire — marks the schedule due now so the next tick fires it
    (subject to authz + idempotency like any other fire)."""
    from axiom.extensions.builtins.schedule import store

    at = now or datetime.now(UTC)
    store.set_next_fire_at(str(schedule_id), at)
    return ScheduleReceipt(schedule_id=schedule_id, action_taken="fire_now", at=at)


def skip_next(schedule_id: ScheduleId, *, now: Optional[datetime] = None) -> ScheduleReceipt:
    """Skip the next occurrence without pausing the series — advance to the
    instant *after* the current next fire. On a one-shot this drops the only
    fire (next_fire_at → NULL)."""
    from axiom.extensions.builtins.schedule import hooks, store
    from axiom.extensions.builtins.schedule.cadence import compute_next_fire_at
    from axiom.extensions.builtins.schedule.db_models import ScheduleDefinition

    at = now or datetime.now(UTC)
    with store.session_scope() as s:
        row = s.get(ScheduleDefinition, str(schedule_id))
        if row is None:
            raise KeyError(f"no such schedule: {schedule_id}")
        cur = row.next_fire_at
        cadence = cadence_from_payload(row.cadence_kind, row.cadence_payload)
        row.next_fire_at = (
            compute_next_fire_at(cadence, last_fire=cur, now=cur) if cur else None
        )
        s.commit()
    hooks.emit(hooks.ON_RESCHEDULE, {"schedule_id": str(schedule_id), "reason": "skip_next"})
    return ScheduleReceipt(schedule_id=schedule_id, action_taken="skip_next", at=at)


def snooze(
    schedule_id: ScheduleId,
    *,
    until: Optional[datetime] = None,
    delay: Optional[timedelta] = None,
    now: Optional[datetime] = None,
) -> ScheduleReceipt:
    """Delay the next fire — to an explicit ``until`` time, or ``delay`` past the
    current next fire. The cadence is unchanged; only this next fire moves."""
    from axiom.extensions.builtins.schedule import hooks, store
    from axiom.extensions.builtins.schedule.db_models import ScheduleDefinition

    at = now or datetime.now(UTC)
    if until is None and delay is None:
        raise ValueError("snooze requires until= or delay=")
    with store.session_scope() as s:
        row = s.get(ScheduleDefinition, str(schedule_id))
        if row is None:
            raise KeyError(f"no such schedule: {schedule_id}")
        if until is not None:
            row.next_fire_at = until
        else:
            base = row.next_fire_at or at
            if base.tzinfo is None:
                base = base.replace(tzinfo=UTC)
            row.next_fire_at = base + delay
        s.commit()
    hooks.emit(hooks.ON_RESCHEDULE, {"schedule_id": str(schedule_id), "reason": "snooze"})
    return ScheduleReceipt(schedule_id=schedule_id, action_taken="snooze", at=at)


def replay_dead_letter(schedule_id: ScheduleId, *, now: Optional[datetime] = None) -> ScheduleReceipt:
    """Re-fire the most recently dead-lettered instant after the cause is fixed.

    Releases that instant's claim and re-arms ``next_fire_at`` to it, so the
    engine re-fires it (with a fresh claim) on the next tick."""
    from axiom.extensions.builtins.schedule import hooks, store
    from axiom.extensions.builtins.schedule.db_models import (
        ScheduleDefinition,
        ScheduleFireLog,
    )

    at = now or datetime.now(UTC)
    with store.session_scope() as s:
        row = (
            s.query(ScheduleFireLog)
            .filter(ScheduleFireLog.schedule_id == str(schedule_id))
            .filter(ScheduleFireLog.outcome == "dead_letter")
            .order_by(ScheduleFireLog.intended_fire_at.desc())
            .first()
        )
        if row is None:
            return ScheduleReceipt(schedule_id=schedule_id, action_taken="replay_noop", at=at)
        intended = row.intended_fire_at
        s.delete(row)  # release the claim so the instant can re-fire
        defn = s.get(ScheduleDefinition, str(schedule_id))
        if defn is not None:
            defn.next_fire_at = intended
        s.commit()
    hooks.emit(
        hooks.ON_RESCHEDULE,
        {"schedule_id": str(schedule_id), "reason": "replay_dead_letter"},
    )
    return ScheduleReceipt(schedule_id=schedule_id, action_taken="replay", at=at)


__all__ = [
    "Cadence",
    "ScheduleId",
    "ScheduleReceipt",
    "ScheduleStatus",
    "ScheduleSummary",
    "cancel",
    "fire_now",
    "list_schedules",
    "pause",
    "register",
    "replay_dead_letter",
    "reschedule",
    "resume",
    "skip_next",
    "snooze",
    "status",
]
