# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""PULSE chaos library — reusable fault injectors and helpers.

Not a CLI; import and call it where a resilience test or a deliberate
fault-injection run is needed. Because the engine is fully dependency-injected
(clock, authz, executor, fire_log, lease are Protocols), most chaos is
deterministic: drive a synthetic clock and swap in a misbehaving double, then
assert the invariants (exactly-once, no double-execute across a crash, bounded
catch-up) hold.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


class JumpClock:
    """A synthetic clock you can step, jump forward, or rewind at will."""

    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def set(self, t: datetime) -> None:
        self.t = t

    def jump(self, **kw: Any) -> None:
        self.t = self.t + timedelta(**kw)


class CountingExecutor:
    """Records every (action, envelope) it runs — for exactly-once assertions."""

    def __init__(self) -> None:
        self.runs: list[tuple] = []

    def run(self, action: str, envelope: Any) -> str:
        self.runs.append((action, envelope))
        return f"r{len(self.runs)}"


class FlakyExecutor:
    """Fails the first ``fail_times`` runs, then succeeds — for retry tests."""

    def __init__(self, fail_times: int = 1, exc: Exception | None = None) -> None:
        self.fail_times = fail_times
        self.exc = exc or RuntimeError("flaky")
        self.runs = 0

    def run(self, action: str, envelope: Any) -> str:
        self.runs += 1
        if self.runs <= self.fail_times:
            raise self.exc
        return f"r{self.runs}"


class CrashAfterExecuteFireLog:
    """Wraps a real FireLog. ``claim`` and execution succeed, but
    ``record_outcome`` raises the first ``crash_times`` calls — simulating a
    crash *after* the action ran, *before* the receipt was written. The
    ``pending`` claim row is left behind for startup reconciliation, and the
    claim guarantees the same instant can never re-execute.
    """

    def __init__(self, real: Any, crash_times: int = 1) -> None:
        self.real = real
        self.crash_times = crash_times
        self.records = 0

    def claim(self, *a: Any, **k: Any) -> bool:
        return self.real.claim(*a, **k)

    def record_skipped(self, *a: Any, **k: Any) -> None:
        return self.real.record_skipped(*a, **k)

    def record_outcome(self, *a: Any, **k: Any) -> None:
        self.records += 1
        if self.records <= self.crash_times:
            raise RuntimeError("crash after execute, before record")
        return self.real.record_outcome(*a, **k)


def drain(ctx: Any, *, max_ticks: int = 1000) -> int:
    """Tick until a tick fires nothing (the backlog is drained). Returns the
    total number fired. Bounded to catch a runaway catch-up loop."""
    from axiom.extensions.builtins.schedule.engine import tick

    total = 0
    for _ in range(max_ticks):
        fired = tick(ctx).fired
        total += fired
        if fired == 0:
            return total
    raise AssertionError(f"drain did not settle within {max_ticks} ticks")


__all__ = [
    "CountingExecutor",
    "CrashAfterExecuteFireLog",
    "FlakyExecutor",
    "JumpClock",
    "drain",
]
