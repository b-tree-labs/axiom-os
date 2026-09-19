# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Fine-grained operator control over a live schedule: skip_next, snooze,
dead-letter replay, and time-slot cancel cascade."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from axiom.extensions.builtins.schedule import chaos, seam, store
from axiom.extensions.builtins.schedule.api import (
    Cadence,
    register,
    replay_dead_letter,
    skip_next,
    snooze,
    status,
)
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


def _hourly():
    return register(envelope={"cap": "x"}, cadence=Cadence(kind="interval", interval=HOUR),
                    action="demo.tick", now=T0)


def test_skip_next_skips_one_occurrence(sqlite_store):
    sched = _hourly()  # next fire at T0+1h
    skip_next(sched, now=T0)
    assert _n(status(sched).summary.next_fire_at) == _n(T0 + 2 * HOUR)  # T0+1h skipped


def test_snooze_until_explicit_time(sqlite_store):
    sched = register(envelope={"cap": "x"}, cadence=Cadence(kind="one_shot"),
                     action="demo.x", now=T0)  # next = T0
    snooze(sched, until=T0 + 3 * HOUR, now=T0)
    assert _n(status(sched).summary.next_fire_at) == _n(T0 + 3 * HOUR)


def test_snooze_delay_from_current_next(sqlite_store):
    sched = _hourly()  # next = T0+1h
    snooze(sched, delay=timedelta(minutes=30), now=T0)
    assert _n(status(sched).summary.next_fire_at) == _n(T0 + HOUR + timedelta(minutes=30))


def test_replay_dead_letter_refires_after_fix(sqlite_store):
    sched = register(envelope={"cap": "x"}, cadence=Cadence(kind="one_shot"),
                     action="demo.once", retry_policy={"max_attempts": 1}, now=T0)
    clock = chaos.JumpClock(T0)

    # First fire fails → dead-letter.
    tick(_ctx(clock, chaos.FlakyExecutor(fail_times=1)))
    assert status(sched).last_outcome == "dead_letter"

    # Cause fixed → replay re-arms the instant; a clean tick re-fires it.
    replay_dead_letter(sched, now=T0)
    good = chaos.CountingExecutor()
    tick(_ctx(clock, good))
    assert len(good.runs) == 1
    assert status(sched).last_outcome == "success"


def test_cancel_time_slot_cascades_to_all_cadences(sqlite_store):
    tsid = seam.register_time_slot(planned_start=T0 + 2 * HOUR, now=T0)
    reminder = seam.register_cadence(  # planned-relative
        cadence=Cadence(kind="one_shot", not_before=T0 + HOUR),
        action="demo.reminder", time_slot_id=tsid, now=T0,
    )
    decay = seam.register_cadence(  # actual-anchored
        cadence=Cadence(kind="one_shot"), action="demo.decay", time_slot_id=tsid,
        anchor_to="actual_end", anchor_offset=HOUR, now=T0,
    )

    out = seam.cancel_time_slot(tsid, now=T0)

    assert out["state"] == "cancelled"
    assert str(reminder) in out["cascaded_cadences"]
    assert str(decay) in out["cascaded_cadences"]
    assert status(reminder).summary.state == "cancelled"
    assert status(decay).summary.state == "cancelled"
