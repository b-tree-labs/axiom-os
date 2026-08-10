# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""The anchor: a dependent cadence fires relative to a slot's *actual* time,
recomputed when the actual is recorded (e.g. a window 24h after actual end)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from axiom.extensions.builtins.schedule import chaos, seam, store
from axiom.extensions.builtins.schedule.api import Cadence, status
from axiom.extensions.builtins.schedule.engine import EngineContext, tick
from axiom.extensions.builtins.schedule.lease import LeaseManager
from axiom.extensions.builtins.schedule.store import SqlFireLog

T0 = datetime(2026, 6, 8, 9, 0, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _n(dt):
    return dt.replace(tzinfo=None) if dt is not None and dt.tzinfo else dt


def _ctx(clock, executor):
    return EngineContext(
        session=store.session_scope,
        authz=type("A", (), {"decide": lambda self, e: True})(),
        fire_log=SqlFireLog(),
        executor=executor,
        lease=LeaseManager(node_id="n", ttl_seconds=30),
        now_fn=clock,
        window_seconds=60,
    )


def test_anchored_timer_is_dormant_then_fires_relative_to_actual(sqlite_store):
    planned = T0 + 2 * HOUR
    tsid = seam.register_time_slot(planned_start=planned, now=T0)
    decay = seam.register_cadence(
        cadence=Cadence(kind="one_shot"),
        action="demo.count_window",
        time_slot_id=tsid,
        anchor_to="actual_end",
        anchor_offset=24 * HOUR,
        now=T0,
    )
    # Dormant until the actual is recorded.
    assert status(decay).summary.next_fire_at is None

    # The event actually ran 30 min long.
    actual_end = planned + timedelta(minutes=30)
    seam.record_actual(tsid, actual_start=planned, actual_end=actual_end)

    # The window now opens 24h after the ACTUAL end, not the planned end.
    assert _n(status(decay).summary.next_fire_at) == _n(actual_end + 24 * HOUR)


def test_anchor_stays_dormant_until_required_actual_present(sqlite_store):
    tsid = seam.register_time_slot(planned_start=T0 + 2 * HOUR, now=T0)
    decay = seam.register_cadence(
        cadence=Cadence(kind="one_shot"), action="x", time_slot_id=tsid,
        anchor_to="actual_end", anchor_offset=HOUR, now=T0,
    )
    # Only the start is recorded; an end-anchored timer can't arm yet.
    seam.record_actual(tsid, actual_start=T0 + 2 * HOUR)
    assert status(decay).summary.next_fire_at is None


def test_anchored_timer_actually_fires_at_computed_time(sqlite_store):
    tsid = seam.register_time_slot(planned_start=T0, now=T0)
    seam.register_cadence(
        cadence=Cadence(kind="one_shot"), action="demo.window", time_slot_id=tsid,
        anchor_to="actual_end", anchor_offset=HOUR, now=T0,
    )
    actual_end = T0
    seam.record_actual(tsid, actual_start=T0, actual_end=actual_end)

    ex = chaos.CountingExecutor()
    clock = chaos.JumpClock(actual_end + HOUR)  # the armed fire time
    assert tick(_ctx(clock, ex)).fired == 1
    assert len(ex.runs) == 1


def test_planned_reschedule_moves_reminder_not_anchored_timer(sqlite_store):
    planned = T0 + 2 * HOUR
    tsid = seam.register_time_slot(planned_start=planned, now=T0)
    reminder = seam.register_cadence(  # planned-relative (slot's primary cadence)
        cadence=Cadence(kind="one_shot", not_before=T0 + HOUR),
        action="demo.reminder", time_slot_id=tsid, now=T0,
    )
    decay = seam.register_cadence(  # actual-anchored (dormant)
        cadence=Cadence(kind="one_shot"), action="demo.decay", time_slot_id=tsid,
        anchor_to="actual_end", anchor_offset=HOUR, now=T0,
    )

    seam.reschedule_time_slot(tsid, new_planned_start=planned + 3 * HOUR, now=T0)

    # The planned-relative reminder followed the move...
    assert _n(status(reminder).summary.next_fire_at) == _n(T0 + HOUR + 3 * HOUR)
    # ...but the actual-anchored timer is untouched (still dormant until actual).
    assert status(decay).summary.next_fire_at is None
