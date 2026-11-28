# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""A restarting engine recovers the fires whose owner died, with no signal.

A scheduled fire records the process that claimed it (pid + that process's
start time). On its own start the engine reads that record: owner dead means
the fire is an orphan at ANY age (a plain systemd restart after ``kill -9``
sends nothing, and the claim would otherwise wedge the job until it aged out);
owner alive means a copy is still working and the fire is left alone; no
recorded owner falls back to the age rule.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.schedule import store
from axiom.extensions.builtins.schedule.api import Cadence, register
from axiom.extensions.builtins.schedule.db_models import ScheduleDefinition, ScheduleFireLog
from axiom.extensions.builtins.schedule.recovery import (
    current_owner,
    owner_is_alive,
    reconcile_pending,
)
from axiom.extensions.builtins.schedule.store import SqlFireLog
from axiom.infra import process_registry

T0 = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)
MINUTE = timedelta(minutes=1)


@pytest.fixture
def sleeper():
    """A live child process, and its identity as a fire claim would record it."""
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        info = None
        for _ in range(50):
            info = process_registry.probe(p.pid)
            if info:
                break
        assert info is not None
        yield p, info.start
    finally:
        if p.poll() is None:
            p.kill()
        p.wait()


def _dead_owner() -> tuple[int, float]:
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    info = None
    for _ in range(50):
        info = process_registry.probe(p.pid)
        if info:
            break
    assert info is not None
    os.kill(p.pid, signal.SIGKILL)
    p.wait()
    return p.pid, info.start


def _claim(owner: tuple[int | None, float | None], *, at=T0, reentrant=False):
    sid = str(register(envelope={"cap": "x"}, cadence=Cadence(kind="interval", interval=MINUTE),
                       action="demo.tick", reentrant=reentrant, now=at - MINUTE))
    assert SqlFireLog().claim(sid, 1, "h", at)
    with store.session_scope() as s:
        row = s.query(ScheduleFireLog).filter_by(schedule_id=sid).one()
        row.owner_pid, row.owner_started_at = owner
        s.commit()
    return sid


def _outcome(sid):
    with store.session_scope() as s:
        return s.query(ScheduleFireLog).filter_by(schedule_id=sid).one().outcome


def test_a_claim_records_the_process_that_made_it(sqlite_store):
    sid = str(register(envelope={"cap": "x"}, cadence=Cadence(kind="interval", interval=MINUTE),
                       action="demo.tick", now=T0))
    SqlFireLog().claim(sid, 1, "h", T0)
    with store.session_scope() as s:
        row = s.query(ScheduleFireLog).filter_by(schedule_id=sid).one()
        assert row.owner_pid == os.getpid()
        assert row.owner_started_at is not None
    assert current_owner()[0] == os.getpid()


def test_a_fire_whose_owner_is_dead_is_interrupted_at_once_whatever_its_age(sqlite_store):
    sid = _claim(_dead_owner())
    # Five seconds old: far under the 300 s age rule that left it wedged.
    out = reconcile_pending(T0 + timedelta(seconds=5), owner_aware=True)
    assert out["flagged"] == [sid]
    assert _outcome(sid) == "interrupted"
    with store.session_scope() as s:
        nxt = s.get(ScheduleDefinition, sid).next_fire_at
    assert nxt is not None and nxt.replace(tzinfo=UTC) > T0 + timedelta(seconds=5), (
        "the schedule moves past the wedged instant, so the next fire is due within one cadence"
    )
    assert nxt.replace(tzinfo=UTC) <= T0 + timedelta(seconds=5) + MINUTE


def test_a_dead_owners_reentrant_fire_has_its_claim_released(sqlite_store):
    sid = _claim(_dead_owner(), reentrant=True)
    out = reconcile_pending(T0 + timedelta(seconds=5), owner_aware=True)
    assert out["reran"] == [sid]
    with store.session_scope() as s:
        assert s.query(ScheduleFireLog).filter_by(schedule_id=sid).count() == 0


def test_a_fire_whose_owner_is_alive_is_left_alone_even_when_old(sqlite_store, sleeper):
    proc, start = sleeper
    sid = _claim((proc.pid, start))
    out = reconcile_pending(T0 + timedelta(hours=2), owner_aware=True)
    assert out == {"reran": [], "flagged": []}
    assert _outcome(sid) == "pending", "a copy still draining keeps its in-flight fire"


def test_a_reused_pid_is_not_the_owner(sqlite_store, sleeper):
    proc, start = sleeper
    # The pid is alive, but it is not the process that claimed the fire.
    sid = _claim((proc.pid, start - 3600))
    assert owner_is_alive(proc.pid, start - 3600) is False
    out = reconcile_pending(T0 + timedelta(seconds=5), owner_aware=True)
    assert out["flagged"] == [sid]


def test_a_row_with_no_recorded_owner_still_waits_for_its_age(sqlite_store):
    sid = _claim((None, None))
    assert owner_is_alive(None, None) is None
    young = reconcile_pending(T0 + timedelta(seconds=60), owner_aware=True)
    assert young == {"reran": [], "flagged": []} and _outcome(sid) == "pending"
    old = reconcile_pending(T0 + timedelta(seconds=600), owner_aware=True)
    assert old["flagged"] == [sid]


def test_the_signal_path_is_unchanged(sqlite_store, sleeper):
    """The supervisor's SIGUSR1 (age 0, not owner-aware) still recovers every pending row."""
    proc, start = sleeper
    sid = _claim((proc.pid, start))
    out = reconcile_pending(T0 + timedelta(seconds=1), stale_after_seconds=0)
    assert out["flagged"] == [sid]


def test_the_orchestrator_reconciles_by_owner_on_its_own_start(sqlite_store, tmp_path, monkeypatch):
    """No SIGUSR1 anywhere: the start-up pass of ``run_forever`` recovers the orphan."""
    import threading

    from axiom.extensions.builtins.data_platform.orchestration import service as orch

    for name in ("ensure_backup_cadences", "ensure_freshness_cadence", "ensure_probe_cadence",
                 "ensure_refresh_cadences", "ensure_adoption_cadences", "refresh_freshness_report"):
        monkeypatch.setattr(orch.OrchestratorService, name, lambda self, *a, **k: {"stale": []})
    sid = _claim(_dead_owner())
    svc = orch.OrchestratorService(registry=object(), state_dir=tmp_path,
                                   clock=lambda: T0 + timedelta(seconds=5))
    assert not svc._sole.is_set()
    stopped = threading.Event()
    stopped.set()
    svc.run_forever(stop_event=stopped)
    assert _outcome(sid) == "interrupted"
