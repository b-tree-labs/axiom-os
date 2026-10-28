# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Time-slot mutation: moving a slot carries its linked cadence along."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from axiom.extensions.builtins.schedule import seam
from axiom.extensions.builtins.schedule.api import Cadence, status

T0 = datetime(2026, 6, 8, 9, 0, 0, tzinfo=UTC)


def _n(dt):
    return dt.replace(tzinfo=None) if dt is not None and dt.tzinfo else dt


def test_reschedule_time_slot_cascades_linked_reminder(sqlite_store):
    planned = T0 + timedelta(hours=2)
    tsid = seam.register_time_slot(planned_start=planned, now=T0)

    reminder_at = T0 + timedelta(hours=1)  # one hour before the slot
    sched = seam.register_cadence(
        cadence=Cadence(kind="one_shot", not_before=reminder_at),
        action="demo.reminder",
        time_slot_id=tsid,
        now=T0,
    )

    # Operator moves the slot 3 hours later.
    new_planned = planned + timedelta(hours=3)
    result = seam.reschedule_time_slot(tsid, new_planned_start=new_planned, now=T0)

    # Slot moved...
    assert _n(result["planned_start"]) == _n(new_planned)
    # ...and the reminder followed by the same delta (still one hour before).
    assert _n(status(sched).summary.next_fire_at) == _n(reminder_at + timedelta(hours=3))


def test_reschedule_time_slot_without_linked_cadence(sqlite_store):
    tsid = seam.register_time_slot(planned_start=T0 + timedelta(hours=2), now=T0)
    new_planned = T0 + timedelta(hours=6)
    result = seam.reschedule_time_slot(tsid, new_planned_start=new_planned, now=T0)
    assert _n(result["planned_start"]) == _n(new_planned)
    assert result["schedule_id"] is None
