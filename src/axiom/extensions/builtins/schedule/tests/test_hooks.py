# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Lifecycle-hook tests: the pre_fire gate (fail-closed veto) and the
observational emits (success / dead_letter / register / reschedule / actual).

Bus emission is best-effort and tested via the local registry; no live bus is
needed (``hooks.emit`` swallows a missing bus).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from axiom.extensions.builtins.schedule import hooks, seam, store
from axiom.extensions.builtins.schedule.api import (
    Cadence,
    register,
    reschedule,
    status,
)
from axiom.extensions.builtins.schedule.engine import EngineContext, tick
from axiom.extensions.builtins.schedule.lease import LeaseManager
from axiom.extensions.builtins.schedule.store import SqlFireLog

T0 = datetime(2026, 6, 8, 9, 0, 0, tzinfo=UTC)


class _Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class _Rec:
    def __init__(self):
        self.calls = []

    def run(self, action, envelope):
        self.calls.append((action, envelope))
        return f"r{len(self.calls)}"


class _Fail:
    def __init__(self):
        self.calls = 0

    def run(self, action, envelope):
        self.calls += 1
        raise RuntimeError("boom")


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


def _one_shot_due(action="demo.x", **kw):
    return register(envelope={"cap": "x"}, cadence=Cadence(kind="one_shot"),
                    action=action, now=T0, **kw)


def test_pre_fire_veto_skips_execution(sqlite_store):
    clock, ex = _Clock(T0), _Rec()
    seen = []
    hooks.register(hooks.PRE_FIRE, lambda p: seen.append(p) or "skip")
    sched = _one_shot_due()
    clock.t = T0
    assert tick(_ctx(clock, ex)).fired == 0
    assert ex.calls == []  # vetoed — never executed
    assert seen and seen[0]["schedule_id"] == str(sched)
    assert status(sched).last_outcome == "failed"


def test_pre_fire_fail_closed_on_raise(sqlite_store):
    clock, ex = _Clock(T0), _Rec()

    def boom(_p):
        raise RuntimeError("precondition check exploded")

    hooks.register(hooks.PRE_FIRE, boom)
    _one_shot_due()
    clock.t = T0
    assert tick(_ctx(clock, ex)).fired == 0  # fail-closed: don't fire under uncertainty
    assert ex.calls == []


def test_pre_fire_proceed_allows_fire(sqlite_store):
    clock, ex = _Clock(T0), _Rec()
    hooks.register(hooks.PRE_FIRE, lambda _p: True)
    _one_shot_due()
    clock.t = T0
    assert tick(_ctx(clock, ex)).fired == 1
    assert len(ex.calls) == 1


def test_on_success_and_register_emit(sqlite_store):
    clock, ex = _Clock(T0), _Rec()
    fired, registered = [], []
    hooks.register(hooks.ON_SUCCESS, fired.append)
    hooks.register(hooks.ON_REGISTER, registered.append)
    sched = _one_shot_due()
    assert registered and registered[0]["schedule_id"] == str(sched)
    clock.t = T0
    tick(_ctx(clock, ex))
    assert fired and fired[0]["schedule_id"] == str(sched)
    assert fired[0]["receipt"] == "r1"


def test_on_dead_letter_emits(sqlite_store):
    clock, ex = _Clock(T0), _Fail()
    dead = []
    hooks.register(hooks.ON_DEAD_LETTER, dead.append)
    sched = _one_shot_due(retry_policy={"max_attempts": 2})
    clock.t = T0
    tick(_ctx(clock, ex))
    assert ex.calls == 2
    assert dead and dead[0]["schedule_id"] == str(sched)
    assert "boom" in dead[0]["error"]


def test_on_reschedule_emits(sqlite_store):
    moved = []
    hooks.register(hooks.ON_RESCHEDULE, moved.append)
    sched = register(envelope={"cap": "x"},
                     cadence=Cadence(kind="interval", interval=timedelta(hours=1)),
                     action="demo.x", now=T0)
    new_at = T0 + timedelta(hours=5)
    reschedule(sched, next_fire_at=new_at, now=T0)
    assert moved and moved[0]["schedule_id"] == str(sched)
    assert moved[0]["new_next_fire_at"] == new_at


def test_on_actual_recorded_emits(sqlite_store):
    anchored = []
    hooks.register(hooks.ON_ACTUAL_RECORDED, anchored.append)
    tsid = seam.register_time_slot(planned_start=T0 + timedelta(hours=2), now=T0)
    actual = T0 + timedelta(hours=2, minutes=3)
    seam.record_actual(tsid, actual_start=actual)
    assert anchored and anchored[0]["time_slot_id"] == tsid
    assert anchored[0]["actual_start"] == actual


def test_observer_exception_does_not_break_fire(sqlite_store):
    clock, ex = _Clock(T0), _Rec()
    hooks.register(hooks.ON_SUCCESS, lambda _p: (_ for _ in ()).throw(RuntimeError("noisy")))
    _one_shot_due()
    clock.t = T0
    assert tick(_ctx(clock, ex)).fired == 1  # observational hook raised, fire still succeeded
    assert len(ex.calls) == 1
