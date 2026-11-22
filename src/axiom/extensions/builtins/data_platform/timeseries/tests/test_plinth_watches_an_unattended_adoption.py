# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""PLINTH's check on an unattended adoption copy, against a fake ledger.

Each fault is shown, with its quiet counterpart: the same ledger one step
healthier raises nothing. Every fault is attributed to us (ADR-182). A mismatch
is latched and only a person clears it. Nothing is said when no adoption is in
progress.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.data_platform.skills import adopt_watch as skill
from axiom.extensions.builtins.data_platform.timeseries import adopt_service as svc
from axiom.extensions.builtins.data_platform.timeseries import adopt_watch as w
from axiom.extensions.builtins.schedule import store
from axiom.extensions.builtins.schedule.db_models import Base, ScheduleDefinition
from axiom.infra.skills import SkillContext, default_registry

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
TABLE = "s.t"
GIB = 2**30


class FakeSource:
    def __init__(self, ledger=None, service=None):
        self._ledger = ledger
        self._service = service or w.ServiceView("active", NOW - timedelta(minutes=2))

    def ledger(self, job):
        if isinstance(self._ledger, Exception):
            raise self._ledger
        return self._ledger

    def service(self, job):
        return self._service


def healthy(**over) -> w.LedgerView:
    base = dict(table=TABLE, phase="copying", windows_done=4, windows_total=10, rows_estimate=1_000_000,
                rows_copied=400_000, updated_at=NOW - timedelta(minutes=3), bytes_per_row=10.0,
                free_bytes=100 * GIB)
    base.update(over)
    return w.LedgerView(**base)


class Sent:
    def __init__(self):
        self.events = []

    def __call__(self, intent, summary, **kw):
        self.events.append((intent, summary, kw))


@pytest.fixture
def state(tmp_path):
    svc.schedule(tmp_path, TABLE, params={}, every_seconds=300, change_ledger=tmp_path / "c.jsonl")
    job = svc.JobStore(tmp_path).get(TABLE)
    job.created_at = (NOW - timedelta(days=1)).isoformat()
    svc.JobStore(tmp_path).put(job)
    return tmp_path


def kinds(report):
    return sorted(f.kind for f in report.faults)


def run(state, source, sent=None, **kw):
    return w.watch(state, source, now=NOW, publish=sent or Sent(), **kw)


# -- quiet --------------------------------------------------------------------


def test_a_healthy_copy_is_quiet(state):
    sent = Sent()
    r = run(state, FakeSource(healthy()), sent)
    assert r.quiet and sent.events == []
    assert w.heartbeat_section(state) is None and w.findings(state) == []


def test_no_adoption_in_progress_is_quiet(tmp_path):
    sent = Sent()
    assert run(tmp_path, FakeSource(None), sent).quiet and sent.events == []


def test_a_finished_copy_is_quiet_however_old_the_ledger(state):
    done = healthy(windows_done=10, updated_at=NOW - timedelta(days=2))
    assert run(state, FakeSource(done)).quiet
    swapped = healthy(phase="swapped", updated_at=NOW - timedelta(days=2))
    assert run(state, FakeSource(swapped)).quiet


def test_a_job_that_is_done_expects_nothing_to_advance(state):
    job = svc.JobStore(state).get(TABLE)
    job.state = "done"
    svc.JobStore(state).put(job)
    stale = healthy(updated_at=NOW - timedelta(days=2))
    assert run(state, FakeSource(stale, w.ServiceView("paused"))).quiet


def test_an_unreadable_ledger_is_not_a_verdict_on_the_copy(state):
    r = run(state, FakeSource(ConnectionError("database is down")))
    assert r.quiet


# -- stalled ------------------------------------------------------------------


def test_stalled_when_the_ledger_has_not_advanced_while_the_service_is_firing(state):
    sent = Sent()
    r = run(state, FakeSource(healthy(updated_at=NOW - timedelta(hours=1, minutes=1))), sent)
    assert kinds(r) == ["stalled"]
    f = r.faults[0]
    assert f.cause == "ours" and f.severity == "warning" and not f.latched
    assert "1h 1m" in f.message and "4/10" in f.message
    assert [(i, kw["payload"]["cause"]) for i, _, kw in sent.events] == [("data.adoption.stalled", "ours")]


def test_not_stalled_just_inside_the_limit_and_the_limit_is_configurable(state):
    inside = healthy(updated_at=NOW - timedelta(minutes=59))
    assert run(state, FakeSource(inside)).quiet
    assert kinds(run(state, FakeSource(healthy(updated_at=NOW - timedelta(minutes=20))),
                     stall_after=timedelta(minutes=10))) == ["stalled"]


def test_a_copy_that_never_began_stalls_from_when_it_was_scheduled(state):
    assert kinds(run(state, FakeSource(None))) == ["stalled"]
    job = svc.JobStore(state).get(TABLE)
    job.created_at = (NOW - timedelta(minutes=5)).isoformat()
    svc.JobStore(state).put(job)
    store_ = w.FaultStore(state)
    store_.save({})
    assert run(state, FakeSource(None)).quiet


# -- down ---------------------------------------------------------------------


def test_down_when_the_schedule_is_gone_or_paused(state):
    for cadence in ("missing", "paused"):
        w.FaultStore(state).save({})
        r = run(state, FakeSource(healthy(), w.ServiceView(cadence)))
        assert kinds(r) == ["down"], cadence
        assert r.faults[0].severity == "critical" and r.faults[0].cause == "ours"


def test_down_when_nothing_has_fired_it_for_longer_than_a_restart_takes(state):
    ancient = w.ServiceView("active", NOW - timedelta(hours=3))
    r = run(state, FakeSource(healthy(), ancient))
    assert kinds(r) == ["down"] and "3h 0m" in r.faults[0].message


def test_a_restart_in_progress_is_not_down(state):
    """Due every 5 minutes, last fired 14: inside the grace, so not down, not stalled."""
    r = run(state, FakeSource(healthy(), w.ServiceView("active", NOW - timedelta(minutes=14))))
    assert r.quiet


def test_down_masks_stalled_because_a_dead_service_explains_a_still_ledger(state):
    r = run(state, FakeSource(healthy(updated_at=NOW - timedelta(hours=5)), w.ServiceView("paused")))
    assert kinds(r) == ["down"]


# -- mismatch -----------------------------------------------------------------


FAILED = {"window_start": "2026-10-01T00:00:00+00:00", "window_end": "2026-10-01T12:00:00+00:00",
          "message": "row count differs: source 10, copy 9", "at": "x"}


def test_mismatch_is_critical_attributed_to_us_and_urgent(state):
    sent = Sent()
    r = run(state, FakeSource(healthy(last_failure=FAILED)), sent)
    assert kinds(r) == ["mismatch"]
    f = r.faults[0]
    assert f.severity == "critical" and f.cause == "ours" and f.latched
    assert "2026-10-01T00:00:00" in f.message and "row count differs" in f.message
    intent, _, kw = sent.events[0]
    assert intent == "data.adoption.mismatch" and kw["priority"] == "urgent"


def test_a_halted_job_is_a_mismatch_even_when_the_ledger_cannot_be_read(state):
    job = svc.JobStore(state).get(TABLE)
    job.state, job.halted_reason = "halted", "window x did not verify"
    svc.JobStore(state).put(job)
    assert kinds(run(state, FakeSource(ConnectionError("down")))) == ["mismatch"]


def test_a_mismatch_is_never_auto_resolved(state):
    sent = Sent()
    run(state, FakeSource(healthy(last_failure=FAILED)), sent)
    # The window verifies on a later attempt and the ledger looks entirely healthy.
    r = run(state, FakeSource(healthy()), sent)
    assert kinds(r) == ["mismatch"], "it stays until a person clears it"
    assert r.cleared == [] and len(sent.events) == 1, "and it is not announced again"
    assert w.heartbeat_section(state)["faults"][0]["latched"] is True


def test_only_an_acknowledgement_clears_a_mismatch(state):
    run(state, FakeSource(healthy(last_failure=FAILED)))
    assert w.acknowledge(state, TABLE) is True
    assert w.acknowledge(state, TABLE) is False
    assert run(state, FakeSource(healthy())).quiet


# -- space --------------------------------------------------------------------


def test_space_when_the_remaining_windows_need_more_than_is_free(state):
    # 600k rows to go at 10 B each is ~5.7 MiB; with 2x headroom 11 MiB. 8 MiB free is short of the headroom.
    r = run(state, FakeSource(healthy(free_bytes=8 * 2**20)))
    assert kinds(r) == ["space"] and r.faults[0].severity == "warning"
    assert "MiB" in r.faults[0].message


def test_space_is_critical_when_it_cannot_fit_even_without_headroom(state):
    r = run(state, FakeSource(healthy(free_bytes=2**20)))
    assert kinds(r) == ["space"] and r.faults[0].severity == "critical"


def test_enough_space_is_quiet_and_an_unmeasured_ledger_does_not_guess(state):
    assert run(state, FakeSource(healthy(free_bytes=100 * 2**20))).quiet
    assert run(state, FakeSource(healthy(bytes_per_row=None, free_bytes=1))).quiet
    assert run(state, FakeSource(healthy(free_bytes=None))).quiet


# -- delivery, clearing and the health surface --------------------------------


def test_a_fault_is_delivered_once_and_a_self_clearing_one_says_when_it_clears(state):
    sent = Sent()
    bad = FakeSource(healthy(updated_at=NOW - timedelta(hours=2)))
    run(state, bad, sent)
    run(state, bad, sent)
    assert len(sent.events) == 1, "delivered when it appears, not on every pass"
    r = run(state, FakeSource(healthy()), sent)
    assert r.cleared == ["stalled:s.t"] and r.quiet is False
    assert sent.events[-1][0] == "data.adoption.stalled.cleared" and sent.events[-1][2]["priority"] == "low"
    assert w.FaultStore(state).load() == {}


def test_the_faults_reach_the_heartbeat_and_the_health_audit(state):
    run(state, FakeSource(healthy(last_failure=FAILED, updated_at=NOW - timedelta(hours=3))))
    beat = w.heartbeat_section(state)
    assert {f["kind"] for f in beat["faults"]} == {"mismatch", "stalled"}
    assert all(f["cause"] == "ours" for f in beat["faults"])
    found = {f.check: f.severity.value for f in w.findings(state)}
    assert found == {"adoption_mismatch": "critical", "adoption_stalled": "warning"}


def test_a_delivery_that_fails_does_not_stop_the_watch(state):
    def broken(*a, **k):
        raise RuntimeError("herald is down")

    r = w.watch(state, FakeSource(healthy(updated_at=NOW - timedelta(hours=2))), now=NOW, publish=broken)
    assert kinds(r) == ["stalled"], "the fault is still raised and kept"


def test_the_node_health_audit_includes_the_raised_faults(state, monkeypatch):
    from axiom.extensions.builtins.hygiene import node_health

    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    run(state, FakeSource(healthy(last_failure=FAILED)))
    assert any(f.check == "adoption_mismatch" for f in node_health.check_adoption_faults())


# -- the skill and its cadence ------------------------------------------------


@pytest.fixture
def sqlite_store():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, future=True)

    @contextlib.contextmanager
    def provider():
        session = maker()
        try:
            yield session
        finally:
            session.close()

    store.set_provider(provider)
    try:
        yield
    finally:
        store.reset_provider()
        engine.dispose()


def _ctx(state):
    return SkillContext(registry=default_registry(), state_dir=state, logger=logging.getLogger("t"), user_prompt=None)


def test_the_skill_is_quiet_when_healthy_and_speaks_when_not(state, monkeypatch):
    monkeypatch.setattr(w, "_publish_default", Sent())
    ok = skill.run({"source": FakeSource(healthy()), "now": NOW}, _ctx(state))
    assert ok.ok and ok.actions_taken == [] and ok.value["faults"] == []
    bad = skill.run({"source": FakeSource(healthy(last_failure=FAILED)), "now": NOW}, _ctx(state))
    assert bad.ok and len(bad.actions_taken) == 1 and bad.value["raised"] == ["mismatch:s.t"]
    ack = skill.run({"acknowledge_mismatch": TABLE}, _ctx(state))
    assert ack.value == {"acknowledged": True}


def test_the_watch_has_one_cadence_active_only_while_there_is_something_to_watch(tmp_path, sqlite_store):
    def rows():
        with store.session_scope() as s:
            return s.query(ScheduleDefinition).filter_by(action=w.WATCH_ACTION).all()

    assert w.sync_watch_cadence(tmp_path, now=NOW) == "none" and rows() == []
    svc.schedule(tmp_path, TABLE, params={}, change_ledger=tmp_path / "c.jsonl")
    assert w.sync_watch_cadence(tmp_path, now=NOW).startswith("registered")
    assert w.sync_watch_cadence(tmp_path, now=NOW).startswith("unchanged")
    assert len(rows()) == 1 and rows()[0].cadence_payload == {"interval_seconds": w.WATCH_EVERY_SECONDS}
    job = svc.JobStore(tmp_path).get(TABLE)
    job.state = "done"
    svc.JobStore(tmp_path).put(job)
    assert w.sync_watch_cadence(tmp_path, now=NOW) == "paused"
    # A raised fault keeps the watch on after the job is gone.
    w.FaultStore(tmp_path).save({"mismatch:s.t": w.Fault("mismatch", TABLE, "critical", "x", latched=True)})
    assert w.sync_watch_cadence(tmp_path, now=NOW).startswith("resumed")
    assert len(rows()) == 1


def test_the_orchestrator_projects_the_watch_with_the_jobs(tmp_path, sqlite_store):
    from axiom.extensions.builtins.data_platform.orchestration.service import OrchestratorService
    from axiom.infra.skills import SkillRegistry

    svc.schedule(tmp_path, TABLE, params={}, change_ledger=tmp_path / "c.jsonl")
    o = OrchestratorService(registry=SkillRegistry(), state_dir=tmp_path, clock=lambda: NOW)
    report = o.ensure_adoption_cadences()
    assert report["(watch)"].startswith("registered") and report[TABLE].startswith("registered")
