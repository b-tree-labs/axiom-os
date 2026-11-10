# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""The anchor: recompute dependent cadences off a time-slot's *actual* time.

A cadence anchored to a slot (``anchor_time_slot_id``) sits dormant until the
slot's actual time is recorded. ``record_actual`` then calls
``recompute_dependents``, which sets each anchored cadence's fire time to
``actual_<anchor_to> + offset`` — so "open a window 24h after the actual end"
counts from what actually happened, not from the plan. An anchor whose required
actual time isn't recorded yet (e.g. anchored to ``actual_end`` but only the
start is in) is left dormant until it is.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional


def recompute_dependents(
    time_slot_id: str,
    *,
    actual_start: Optional[datetime],
    actual_end: Optional[datetime],
    now: Optional[datetime] = None,
) -> list[str]:
    """Reschedule cadences anchored to this slot. Returns the rescheduled ids."""
    from axiom.extensions.builtins.schedule import store
    from axiom.extensions.builtins.schedule.api import reschedule
    from axiom.extensions.builtins.schedule.db_models import ScheduleDefinition

    with store.session_scope() as s:
        rows = (
            s.query(ScheduleDefinition)
            .filter(ScheduleDefinition.anchor_time_slot_id == time_slot_id)
            .filter(ScheduleDefinition.state == "active")
            .all()
        )
        specs = [(r.id, r.anchor_to, r.anchor_offset_seconds) for r in rows]

    rescheduled: list[str] = []
    for sid, anchor_to, offset in specs:
        base = actual_end if anchor_to == "actual_end" else actual_start
        if base is None:
            continue  # required actual not recorded yet — stay dormant
        reschedule(sid, next_fire_at=base + timedelta(seconds=offset), now=now)
        rescheduled.append(sid)
    return rescheduled


__all__ = ["recompute_dependents"]
