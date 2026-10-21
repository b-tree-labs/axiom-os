# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The follow-up clock: due times registered on PULSE (ADR-196 D2).

No daemon of our own. A follow-up's next action time is a one-shot cadence on
the schedule substrate whose action is the skill ``program.followup_fire``
and whose envelope carries only the follow-up id. PULSE supplies the durable
due-time job, the exactly-once claim and restart recovery; the orchestrator
service that hosts PULSE runs it.

Each registration is its own PULSE row: after a snooze or a rung we cancel the
old row and register a new one, rather than moving a fired row, so a new fire
never collides with an earlier fire's idempotency bucket.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

FIRE_ACTION = "program.followup_fire"
SWEEP_ACTION = "program.followup_sweep"
EXTENSION = "program"
#: PULSE retries a fire that raised; the fire is idempotent (dedup keys), so retry is safe.
FIRE_ATTEMPTS = 3

_log = logging.getLogger("axiom.program.followup_clock")


def arm(followup_id: str, at: datetime, *, now: datetime | None = None) -> str:
    """Register the one-shot for ``followup_id`` at ``at``; returns the PULSE id."""
    from axiom.extensions.builtins.schedule import api as pulse
    from axiom.extensions.builtins.schedule.api import Cadence

    at = at if at.tzinfo else at.replace(tzinfo=UTC)
    sid = pulse.register(
        envelope={"action": FIRE_ACTION, "params": {"followup_id": followup_id}},
        cadence=Cadence(kind="one_shot", not_before=at),
        action=FIRE_ACTION,
        description=f"follow-up {followup_id}",
        extension=EXTENSION,
        retry_policy={"max_attempts": FIRE_ATTEMPTS},
        misfire_policy="fire_once",  # overdue after downtime: fire once on restart
        reentrant=True,  # a fire killed mid-flight is re-run; the fire is idempotent
        now=now,
    )
    return str(sid)


def disarm(schedule_id: str | None) -> None:
    """Cancel a PULSE row. Absent or already-cancelled is not an error."""
    if not schedule_id:
        return
    from axiom.extensions.builtins.schedule import api as pulse

    try:
        pulse.cancel(pulse.ScheduleId(schedule_id))
    except Exception as exc:  # noqa: BLE001 - cancelling a gone row must not block a close
        _log.info("could not cancel schedule %s (%s)", schedule_id, type(exc).__name__)


def rearm(followup_id: str, old_schedule_id: str | None, at: datetime | None) -> str | None:
    """Replace the PULSE row: cancel the old one, register a new one if ``at``."""
    disarm(old_schedule_id)
    return arm(followup_id, at) if at is not None else None


def is_active(schedule_id: str | None) -> bool:
    """Is there a live (active) PULSE row for this id?"""
    if not schedule_id:
        return False
    from axiom.extensions.builtins.schedule import store
    from axiom.extensions.builtins.schedule.db_models import ScheduleDefinition

    with store.session_scope() as s:
        row = s.get(ScheduleDefinition, schedule_id)
        return row is not None and row.state == "active"


def describe(row: dict[str, Any]) -> dict[str, Any]:
    return {"schedule_id": row.get("schedule_id"), "next_action_at": row.get("next_action_at")}


__all__ = ["EXTENSION", "FIRE_ACTION", "SWEEP_ACTION", "arm", "disarm", "is_active", "rearm"]
