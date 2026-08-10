# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Resilience: misfire policy (restart catch-up), startup reconciliation of
interrupted fires, and a chaos proof that a crash between execute and record
cannot double-run the action."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from axiom.extensions.builtins.schedule import chaos, store
from axiom.extensions.builtins.schedule.api import Cadence, register, status
from axiom.extensions.builtins.schedule.engine import EngineContext, tick
from axiom.extensions.builtins.schedule.lease import LeaseManager
from axiom.extensions.builtins.schedule.recovery import reconcile_pending
from axiom.extensions.builtins.schedule.store import SqlFireLog

T0 = datetime(2026, 6, 8, 9, 0, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _n(dt):
    return dt.replace(tzinfo=None) if dt is not None and dt.tzinfo else dt


def _allow_authz():
    return type("A", (), {"decide": lambda self, e: True})()


def _ctx(clock, executor, fire_log=None):
    return EngineContext(
        session=store.session_scope,
        authz=_allow_authz(),
        fire_log=fire_log or SqlFireLog(),
        executor=executor,
        lease=LeaseManager(node_id="n", ttl_seconds=30),
        now_fn=clock,
        window_seconds=60,
    )


def _hourly(misfire):
    # Registered at T0 → first fire at T0+1h. Ticking far later = "was down".
    return register(envelope={"cap": "x"}, cadence=Cadence(kind="interval", interval=HOUR),
                    action="demo.tick", misfire_policy=misfire, now=T0)


def test_misfire_skip_drops_backlog(sqlite_store):
    sched = _hourly("skip")
    clock = chaos.JumpClock(T0 + 5 * HOUR)  # down for hours
    ex = chaos.CountingExecutor()
    tick(_ctx(clock, ex))
    assert ex.runs == []  # missed instant dropped
    assert _n(status(sched).summary.next_fire_at) > _n(T0 + 5 * HOUR)  # jumped to the future


def test_misfire_fire_once_fires_one_then_jumps(sqlite_store):
    sched = _hourly("fire_once")
    clock = chaos.JumpClock(T0 + 5 * HOUR)
    ex = chaos.CountingExecutor()
    total = chaos.drain(_ctx(clock, ex), max_ticks=50)
    assert total == 1 and len(ex.runs) == 1  # one fire, backlog skipped
    assert _n(status(sched).summary.next_fire_at) > _n(T0 + 5 * HOUR)


def test_misfire_fire_all_catches_up(sqlite_store):
    _hourly("fire_all")
    clock = chaos.JumpClock(T0 + 5 * HOUR)
    ex = chaos.CountingExecutor()
    total = chaos.drain(_ctx(clock, ex), max_ticks=50)
    # Instants at T+1h..T+5h were all missed and each runs once.
    assert total == 5 and len(ex.runs) == 5


def test_reconcile_non_reentrant_flags_and_advances(sqlite_store):
    sched = _hourly("fire_once")  # reentrant defaults False
    # Simulate a crash: a stale pending claim with no recorded outcome.
    SqlFireLog().claim(str(sched), 1, "h", T0)
    out = reconcile_pending(T0 + HOUR, stale_after_seconds=60)
    assert str(sched) in out["flagged"] and out["reran"] == []


def test_reconcile_reentrant_releases_claim(sqlite_store):
    sched = register(envelope={"cap": "x"}, cadence=Cadence(kind="interval", interval=HOUR),
                     action="demo.tick", reentrant=True, now=T0)
    SqlFireLog().claim(str(sched), 1, "h", T0)
    out = reconcile_pending(T0 + HOUR, stale_after_seconds=60)
    assert str(sched) in out["reran"] and out["flagged"] == []


def test_chaos_crash_after_execute_runs_action_exactly_once(sqlite_store):
    """The crown jewel: the action runs, the engine crashes before recording,
    and on restart the persisted claim prevents any re-execution."""
    sched = register(envelope={"cap": "x"}, cadence=Cadence(kind="one_shot"),
                     action="demo.once", now=T0)
    ex = chaos.CountingExecutor()
    crashing = chaos.CrashAfterExecuteFireLog(SqlFireLog(), crash_times=1)
    clock = chaos.JumpClock(T0)

    # First tick: action runs, then record_outcome crashes (exception swallowed
    # by tick's per-schedule guard). The pending claim is left behind.
    report = tick(_ctx(clock, ex, fire_log=crashing))
    assert len(ex.runs) == 1
    assert report.fired == 0  # crashed before the success was counted

    # Restart: a fresh tick re-pulls the still-due instant — but the claim
    # already exists, so it is NOT executed again.
    tick(_ctx(clock, ex, fire_log=crashing))
    assert len(ex.runs) == 1  # exactly once, despite the crash

    _ = sched  # registered above; identity used via the claim
