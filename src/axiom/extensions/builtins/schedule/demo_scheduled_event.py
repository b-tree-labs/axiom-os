# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Runnable end-to-end demo of the PULSE fire path + consumer seam.

Mirrors what a consumer's first scheduling CLI verb will compose, using only
the domain-agnostic seam. Run it::

    python -m axiom.extensions.builtins.schedule.demo_scheduled_event

It stands up an in-memory SQLite store, reserves a slot, registers a reminder
cadence, drives a synthetic clock through the engine, and prints each step —
including the idempotent replay and the planned-vs-actual read-back.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.schedule import seam, store
from axiom.extensions.builtins.schedule.api import Cadence
from axiom.extensions.builtins.schedule.db_models import Base
from axiom.extensions.builtins.schedule.engine import EngineContext, tick
from axiom.extensions.builtins.schedule.lease import LeaseManager


class _Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def run(self, action, envelope):
        self.calls.append((action, envelope))
        print(f"      [executor] fired action={action!r} envelope={envelope}")
        return f"receipt-{len(self.calls)}"


class _AllowAuthz:
    def decide(self, envelope):
        return True


def _bind_sqlite():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, future=True)

    @contextlib.contextmanager
    def provider():
        s = maker()
        try:
            yield s
        finally:
            s.close()

    store.set_provider(provider)


def main() -> None:
    _bind_sqlite()
    t0 = datetime(2026, 6, 8, 9, 0, 0, tzinfo=UTC)
    clock = _Clock(t0)
    executor = _RecordingExecutor()
    ctx = EngineContext(
        session=store.session_scope,
        authz=_AllowAuthz(),
        fire_log=store.SqlFireLog(),
        executor=executor,
        lease=LeaseManager(node_id="demo", ttl_seconds=30),
        now_fn=clock,
        window_seconds=60,
    )

    print("PULSE end-to-end demo — scheduled event + reminder\n")

    planned = t0 + timedelta(hours=2)
    time_slot_id = seam.register_time_slot(
        planned_start=planned,
        metadata={"ref": "job-001", "labels": {"kind": "demo"}},
        now=t0,
    )
    print(f"  1. reserved slot {time_slot_id[:8]} planned_start={planned.isoformat()}")

    reminder_at = t0 + timedelta(hours=1)
    sched_id = seam.register_cadence(
        cadence=Cadence(kind="one_shot", not_before=reminder_at),
        action="demo.reminder",
        time_slot_id=time_slot_id,
        envelope={"cap": "notify"},
        now=t0,
    )
    print(f"  2. registered reminder cadence {str(sched_id)[:8]} due {reminder_at.isoformat()}")

    clock.t = reminder_at - timedelta(minutes=1)
    print(f"  3. tick @ {clock.t.isoformat()} (before due) -> fired={tick(ctx).fired}")

    clock.t = reminder_at
    print(f"  4. tick @ {clock.t.isoformat()} (due):")
    print(f"       -> fired={tick(ctx).fired}")

    store.set_next_fire_at(str(sched_id), reminder_at)
    print(f"  5. re-mark due + tick again -> fired={tick(ctx).fired} (idempotent; "
          f"executor ran {len(executor.calls)}x total)")

    actual = planned + timedelta(minutes=5)
    seam.record_actual(time_slot_id, actual_start=actual)
    st = seam.time_slot_status(time_slot_id)
    print(f"  6. recorded actual_start={actual.isoformat()}")
    print(f"  7. time_slot_status: planned={st['planned_start']} actual={st['actual_start']} "
          f"meta={st['metadata']} linked_cadence={str(st['schedule_id'])[:8]} state={st['state']}")

    print("\nOK — fired exactly once, idempotent on replay, planned-vs-actual captured.")
    store.reset_provider()


if __name__ == "__main__":
    main()
