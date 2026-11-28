# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Culminating end-to-end proof of the PULSE fire path + consumer seam.

This is the prelude to a consumer's first scheduling CLI verb (e.g. one that
schedules a future event and a reminder). It exercises, against a real
SQLite-backed engine, the exact composition such a verb performs — using only
the domain-agnostic seam, no consumer naming:

    reserve a slot for a planned event  (register_time_slot)
      + a reminder cadence before it    (register_cadence -> PULSE)
      -> the engine fires it exactly once, idempotently, under authz
      -> record what actually happened   (record_actual)
      -> read back planned-vs-actual     (time_slot_status)

Plus the harden guarantees: idempotent replay, retry -> dead_letter, authz
deny records-but-does-not-execute.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from axiom.extensions.builtins.schedule import seam, store
from axiom.extensions.builtins.schedule.api import Cadence, register, status
from axiom.extensions.builtins.schedule.engine import EngineContext, tick
from axiom.extensions.builtins.schedule.lease import LeaseManager
from axiom.extensions.builtins.schedule.store import SqlFireLog

T0 = datetime(2026, 6, 8, 9, 0, 0, tzinfo=UTC)


def _n(dt):
    """Normalize tz for SQLite round-trip (SQLite drops tzinfo; Postgres keeps it)."""
    return dt.replace(tzinfo=None) if dt is not None and dt.tzinfo else dt


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
        return f"receipt-{len(self.calls)}"


class _FailingExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, action, envelope):
        self.calls += 1
        raise RuntimeError("boom")


class _Authz:
    def __init__(self, allow: bool) -> None:
        self.allow = allow

    def decide(self, envelope):
        return self.allow


def _ctx(clock, executor, *, allow=True, window=60):
    return EngineContext(
        session=store.session_scope,
        authz=_Authz(allow),
        fire_log=SqlFireLog(),
        executor=executor,
        lease=LeaseManager(node_id="node-a", ttl_seconds=30),
        now_fn=clock,
        window_seconds=window,
    )


def test_culminating_proof_scheduled_event_with_reminder(sqlite_store):
    clock = _Clock(T0)
    executor = _RecordingExecutor()
    ctx = _ctx(clock, executor)

    # 1. Reserve a slot for a planned event 2h out (the "Scheduled" record).
    planned = T0 + timedelta(hours=2)
    time_slot_id = seam.register_time_slot(
        planned_start=planned,
        metadata={"ref": "job-001", "labels": {"kind": "demo", "where": "bay-3"}},
        now=T0,
    )

    # 2. Register a one-shot reminder 1h before the event, linked to the slot.
    reminder_at = T0 + timedelta(hours=1)
    sched_id = seam.register_cadence(
        cadence=Cadence(kind="one_shot", not_before=reminder_at),
        action="demo.reminder",
        time_slot_id=time_slot_id,
        envelope={"cap": "notify"},
        now=T0,
    )

    # 3. A tick before the reminder is due fires nothing.
    clock.t = reminder_at - timedelta(minutes=1)
    assert tick(ctx).fired == 0
    assert executor.calls == []

    # 4. A tick at the due instant fires the reminder exactly once.
    clock.t = reminder_at
    assert tick(ctx).fired == 1
    assert len(executor.calls) == 1
    assert executor.calls[0][0] == "demo.reminder"

    # 5. Idempotency: re-mark due and tick again — the claim is rejected, no
    #    double fire. (Same schedule + same fire-time bucket + same params.)
    store.set_next_fire_at(str(sched_id), reminder_at)
    assert tick(ctx).fired == 0
    assert len(executor.calls) == 1

    # 6. Record what actually happened (event ran 5 min late) — planned vs actual.
    actual = planned + timedelta(minutes=5)
    seam.record_actual(time_slot_id, actual_start=actual)

    # 7. Read back: planned, actual, opaque metadata, linked cadence, state.
    st = seam.time_slot_status(time_slot_id)
    assert _n(st["planned_start"]) == _n(planned)
    assert _n(st["actual_start"]) == _n(actual)
    assert st["metadata"]["ref"] == "job-001"
    assert st["metadata"]["labels"]["where"] == "bay-3"
    assert st["schedule_id"] == str(sched_id)
    assert st["state"] == "active"


def test_retry_then_dead_letter(sqlite_store):
    clock = _Clock(T0)
    executor = _FailingExecutor()
    ctx = _ctx(clock, executor)

    sched = register(
        envelope={"cap": "x"},
        cadence=Cadence(kind="one_shot"),
        action="demo.always_fails",
        retry_policy={"max_attempts": 3},
        now=T0,
    )
    clock.t = T0
    report = tick(ctx)

    assert report.fired == 0
    assert executor.calls == 3  # retried up to max_attempts
    st = status(sched)
    assert st.last_outcome == "dead_letter"
    assert st.dead_letter_count == 1


def test_authz_deny_records_without_executing(sqlite_store):
    clock = _Clock(T0)
    executor = _RecordingExecutor()
    ctx = _ctx(clock, executor, allow=False)

    sched = register(
        envelope={"cap": "x"},
        cadence=Cadence(kind="one_shot"),
        action="demo.denied",
        now=T0,
    )
    clock.t = T0
    assert tick(ctx).fired == 0
    assert executor.calls == []  # never executed
    assert status(sched).last_outcome == "failed"
