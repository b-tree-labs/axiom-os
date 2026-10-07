# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Engine-tick tests with a synthetic clock.

Per spec-axiom-schedule §3 + §1: the tick loop polls due rows under the
lease and dispatches each through ``_fire_one``. These tests verify the
*loop control flow* — not-leader skip, lease auto-acquire, lease
renewal cadence. The DB-backed ``_pull_due`` + ``_fire_one`` integration
test lands once the schema migrations run in CI.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any


from axiom.extensions.builtins.schedule.engine import EngineContext, tick
from axiom.extensions.builtins.schedule.lease import LeaseManager


T0 = datetime(2026, 5, 31, 12, 0, 0, tzinfo=UTC)


class _Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: int) -> None:
        self.t = self.t + timedelta(seconds=seconds)


class _NullSession:
    def __call__(self) -> Any:
        return None


class _NullAuthz:
    def decide(self, envelope: Any) -> Any:
        raise AssertionError("authz must not be called when no rows are due")


class _NullFireLog:
    def claim(self, *args, **kwargs) -> bool:
        return False

    def record_skipped(self, *args, **kwargs) -> None:
        pass

    def record_outcome(self, *args, **kwargs) -> None:
        pass


class _NullExecutor:
    def run(self, action: str, envelope: Any) -> Any:
        raise AssertionError("executor must not be called when no rows are due")


def _ctx(clock: _Clock, mgr: LeaseManager) -> EngineContext:
    return EngineContext(
        session=_NullSession(),
        authz=_NullAuthz(),
        fire_log=_NullFireLog(),
        executor=_NullExecutor(),
        lease=mgr,
        now_fn=clock,
    )


def test_tick_acquires_lease_when_not_held(monkeypatch):
    """First tick must auto-acquire the lease so single-node engines start
    firing without an explicit acquire() call."""
    clock = _Clock(T0)
    mgr = LeaseManager(node_id="node-a", ttl_seconds=30)
    ctx = _ctx(clock, mgr)

    # Stub _pull_due to return [] so we exercise the control flow without DB.
    from axiom.extensions.builtins.schedule import engine as eng
    monkeypatch.setattr(eng, "_pull_due", lambda c, n: [])

    report = tick(ctx)
    assert mgr.held(T0)
    assert report.fired == 0
    assert report.skipped is None


def test_tick_renews_lease_when_threshold_crossed(monkeypatch):
    clock = _Clock(T0)
    mgr = LeaseManager(node_id="node-a", ttl_seconds=30)
    mgr.try_acquire(T0)
    ctx = _ctx(clock, mgr)

    from axiom.extensions.builtins.schedule import engine as eng
    monkeypatch.setattr(eng, "_pull_due", lambda c, n: [])

    # Advance past the renewal threshold (ttl/3 = 10s).
    clock.advance(11)
    tick(ctx)
    # Lease should now extend past the original 30s window.
    assert mgr.held(T0 + timedelta(seconds=40))
