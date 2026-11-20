# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Policy & safety windows: compliance-window outcome, blackout windows, and
the register-time allocation gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.schedule import blackout, chaos, hooks, seam, store
from axiom.extensions.builtins.schedule.api import Cadence, register, status
from axiom.extensions.builtins.schedule.engine import EngineContext, tick
from axiom.extensions.builtins.schedule.lease import LeaseManager
from axiom.extensions.builtins.schedule.seam import AllocationError
from axiom.extensions.builtins.schedule.store import SqlFireLog

T0 = datetime(2026, 6, 8, 9, 0, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


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


# --- compliance window ---

def test_compliance_flag_executes_but_records_out_of_window(sqlite_store):
    sched = register(envelope={"cap": "x"}, cadence=Cadence(kind="one_shot"),
                     action="demo.x", compliance_window_seconds=60,
                     compliance_action="flag", now=T0)
    ex = chaos.CountingExecutor()
    tick(_ctx(chaos.JumpClock(T0 + HOUR), ex))  # an hour late
    assert len(ex.runs) == 1                     # flag still executes
    assert status(sched).last_outcome == "out_of_window"  # but the deviation is recorded


def test_compliance_skip_does_not_execute_a_late_fire(sqlite_store):
    sched = register(envelope={"cap": "x"}, cadence=Cadence(kind="one_shot"),
                     action="demo.x", compliance_window_seconds=60,
                     compliance_action="skip", now=T0)
    ex = chaos.CountingExecutor()
    tick(_ctx(chaos.JumpClock(T0 + HOUR), ex))
    assert ex.runs == []                          # skip: don't fire a late instant
    assert status(sched).last_outcome == "out_of_window"


def test_within_compliance_window_fires_normally(sqlite_store):
    sched = register(envelope={"cap": "x"}, cadence=Cadence(kind="one_shot"),
                     action="demo.x", compliance_window_seconds=3600, now=T0)
    ex = chaos.CountingExecutor()
    tick(_ctx(chaos.JumpClock(T0 + timedelta(minutes=30)), ex))  # 30 min late, in window
    assert len(ex.runs) == 1
    assert status(sched).last_outcome == "success"


# --- blackout ---

def test_blackout_suppresses_fire_then_resumes_after(sqlite_store):
    _hourly()  # first fire at T0+1h
    blackout.add_blackout(T0, T0 + 2 * HOUR, reason="maintenance", now=T0)
    ex = chaos.CountingExecutor()

    tick(_ctx(chaos.JumpClock(T0 + HOUR), ex))     # inside the blackout
    assert ex.runs == []

    tick(_ctx(chaos.JumpClock(T0 + 3 * HOUR), ex))  # after it
    assert len(ex.runs) == 1


def test_lift_blackout_reenables_firing(sqlite_store):
    _hourly()
    bid = blackout.add_blackout(T0, T0 + 5 * HOUR, now=T0)
    assert blackout.in_blackout(T0 + HOUR) is True
    assert blackout.lift_blackout(bid) is True
    assert blackout.in_blackout(T0 + HOUR) is False


# --- allocation gate ---

def test_allocation_gate_vetoes_reservation(sqlite_store):
    hooks.register(hooks.PRE_REGISTER, lambda p: "deny")
    with pytest.raises(AllocationError):
        seam.register_time_slot(planned_start=T0 + HOUR, now=T0)


def test_allocation_gate_allows_when_no_veto(sqlite_store):
    assert seam.register_time_slot(planned_start=T0 + HOUR, now=T0)
