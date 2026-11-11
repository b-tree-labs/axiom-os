# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The unattended adoption job, without a database server.

The job is a cadence on the platform's scheduler, not a service of its own: what
needs showing here is the part this adds. The readiness gate is bounded and
loud; the connection target is a setting that does not default to a load
balancer; the job record projects onto PULSE idempotently and a finished job
pauses its own cadence; installing twice changes nothing; the change records
bracket the copy.
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.data_platform.skills import adopt_tick
from axiom.extensions.builtins.data_platform.skills import adopt_timeseries as skill
from axiom.extensions.builtins.data_platform.timeseries import adopt_service as svc
from axiom.extensions.builtins.schedule import store
from axiom.extensions.builtins.schedule.db_models import Base, ScheduleDefinition
from axiom.infra.skills import SkillContext, default_registry

T0 = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


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


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_CHANGE_LEDGER", str(tmp_path / "changes.jsonl"))
    for name in (svc.DSN_ENV, svc.DIRECT_DSN_ENV):
        monkeypatch.delenv(name, raising=False)


def _rows(action=svc.TICK_ACTION):
    with store.session_scope() as s:
        return s.query(ScheduleDefinition).filter_by(action=action).all()


class _Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


# -- the readiness gate -------------------------------------------------------


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *_):
        return self


def test_the_gate_waits_until_the_database_accepts_a_connection():
    clock, tries = _Clock(), []

    def connect(dsn, **kw):
        tries.append(dsn)
        if len(tries) < 4:
            raise ConnectionError("the database system is starting up")
        return _Conn()

    attempts = svc.wait_for_database("db", timeout_s=60, interval_s=2, connect=connect,
                                     clock=clock, sleep=clock.sleep)
    assert attempts == 4 and clock.slept == [2, 2, 2]


def test_the_gate_is_bounded_and_says_what_it_waited_for(caplog):
    clock = _Clock()

    def connect(dsn, **kw):
        raise ConnectionError("connection refused")

    with caplog.at_level("ERROR"), pytest.raises(svc.DatabaseNotReady) as e:
        svc.wait_for_database("db", timeout_s=10, interval_s=2, connect=connect,
                              clock=clock, sleep=clock.sleep)
    assert "10s" in str(e.value) and "connection refused" in str(e.value)
    assert clock.t <= 12, "it gave up at the bound and did not keep waiting"
    assert any("did not accept a connection" in r.message for r in caplog.records), "loud on timeout"


def test_a_fire_against_a_database_that_never_comes_up_is_an_outcome_not_a_crash(tmp_path):
    clock = _Clock()
    job = svc.Job(table="s.t", ready_timeout_seconds=6, params={"dsn": "postgresql://nowhere/x"})
    st = svc.JobStore(tmp_path)
    st.put(job)

    def connect(dsn, **kw):
        raise ConnectionError("refused")

    from axiom.extensions.builtins.data_platform.timeseries.contract import TimeseriesContract

    out = svc.run_tick(job, TimeseriesContract(schema="s", table="t", identity=("a",)), st,
                       connect=connect, clock=clock, sleep=clock.sleep)
    assert out.status == "not_ready" and not out.ok
    assert "did not accept a connection" in out.message
    assert st.get("s.t").state == "scheduled", "a database that is down does not end the job"


# -- the connection target ----------------------------------------------------


def test_the_target_is_a_setting_in_a_stated_order(monkeypatch):
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://platform:5432/p")
    assert svc.direct_dsn(None)[1] == "platform default"
    monkeypatch.setenv(svc.DIRECT_DSN_ENV, "postgresql://db.internal:5432/p")
    assert svc.direct_dsn(None) == ("postgresql://db.internal:5432/p", svc.DIRECT_DSN_ENV)
    monkeypatch.setenv(svc.DSN_ENV, "postgresql://direct:5432/p")
    assert svc.direct_dsn(None) == ("postgresql://direct:5432/p", svc.DSN_ENV)
    job = svc.Job(table="s.t", params={"dsn": "postgresql://job:5432/p"})
    assert svc.direct_dsn(job) == ("postgresql://job:5432/p", "job")


def test_reaching_the_database_through_a_loopback_default_is_called_out(monkeypatch):
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://u:p@localhost:5432/d")
    dsn, source = svc.direct_dsn(None)
    warning = svc.warn_if_balancer(dsn, source)
    assert warning and "600 s" in warning and svc.DSN_ENV in warning
    assert svc.warn_if_balancer("postgresql://db.internal/d", "platform default") is None
    assert svc.warn_if_balancer("postgresql://localhost/d", svc.DSN_ENV) is None, "a stated choice is not second-guessed"


def test_the_connection_carries_tcp_keepalives():
    assert "keepalives=1" in svc._with_keepalives("postgresql://h/d")
    assert svc._with_keepalives("postgresql://h/d?sslmode=disable").count("?") == 1
    assert svc._with_keepalives("host=h dbname=d").endswith("keepalives_count=3")
    assert svc._with_keepalives("postgresql://h/d?keepalives=0") == "postgresql://h/d?keepalives=0"


# -- the job on the scheduler -------------------------------------------------


def test_installing_twice_is_one_job_and_one_cadence(tmp_path, sqlite_store):
    for _ in range(2):
        svc.schedule(tmp_path, "s.t", params={"identity": "a,b"}, every_seconds=300)
        svc.sync_cadences(tmp_path, now=T0)
    assert len(svc.JobStore(tmp_path).load()) == 1
    rows = _rows()
    assert len(rows) == 1 and rows[0].state == "active"
    assert rows[0].cadence_payload == {"interval_seconds": 300}
    assert rows[0].capability_envelope["params"] == {"table": "s.t"}


def test_a_changed_interval_retimes_the_same_cadence(tmp_path, sqlite_store):
    svc.schedule(tmp_path, "s.t", params={}, every_seconds=300)
    first = svc.sync_cadences(tmp_path, now=T0)["s.t"]
    svc.schedule(tmp_path, "s.t", params={}, every_seconds=60)
    assert svc.sync_cadences(tmp_path, now=T0)["s.t"].startswith("rescheduled")
    rows = _rows()
    assert len(rows) == 1 and rows[0].cadence_payload == {"interval_seconds": 60}
    assert first.startswith("registered")


def test_a_done_job_pauses_its_own_cadence_and_reinstalling_resumes_it(tmp_path, sqlite_store):
    st = svc.JobStore(tmp_path)
    job = svc.schedule(tmp_path, "s.t", params={})
    svc.sync_cadences(tmp_path, now=T0)
    job.state = "done"
    st.put(job)
    assert svc.sync_cadences(tmp_path, now=T0)["s.t"] == "paused"
    assert _rows()[0].state == "paused"
    svc.schedule(tmp_path, "s.t", params={})
    assert svc.sync_cadences(tmp_path, now=T0)["s.t"].startswith("resumed")
    assert _rows()[0].state == "active"


def test_unscheduling_cancels_the_cadence_and_touches_nothing_else(tmp_path, sqlite_store):
    svc.schedule(tmp_path, "s.t", params={})
    svc.sync_cadences(tmp_path, now=T0)
    assert svc.unschedule(tmp_path, "s.t") is True
    assert svc.unschedule(tmp_path, "s.t") is False, "removing what is not there is not an error"
    assert svc.sync_cadences(tmp_path, now=T0) == {"s.t": "cancelled"}
    assert _rows() == [] or all(r.state == "cancelled" for r in _rows())


def test_a_batch_is_short_enough_not_to_starve_the_node_probe(tmp_path):
    job = svc.schedule(tmp_path, "s.t", params={}, change_ledger=tmp_path / "c.jsonl")
    assert job.batch_seconds == 30 and svc.DEFAULT_BATCH_SECONDS < 60


def test_the_chart_exports_the_dsn_only_when_it_is_set():
    import pathlib

    chart = pathlib.Path(svc.__file__).parents[7] / "infra" / "charts" / "axiom-node"
    if not chart.is_dir():
        return
    assert 'adoption:\n  dsn: ""' in (chart / "values.yaml").read_text()
    assert "with .root.Values.adoption.dsn" in (chart / "templates" / "_helpers.tpl").read_text()


def test_the_install_verb_writes_the_job_and_registers_the_cadence(tmp_path, sqlite_store):
    ctx = SkillContext(registry=default_registry(), state_dir=tmp_path, logger=logging.getLogger("t"), user_prompt=None)
    r = skill.run({"table": "s.t", "identity": "a", "schedule": True, "every": "10m",
                   "batch_seconds": 90}, ctx)
    assert r.ok, r.errors
    job = svc.JobStore(tmp_path).get("s.t")
    assert (job.every_seconds, job.batch_seconds, job.params["identity"]) == (600, 90, "a")
    assert any(a.startswith("cadence: registered") for a in r.actions_taken)
    again = skill.run({"table": "s.t", "identity": "a", "schedule": True}, ctx)
    assert again.ok and any(a.startswith("cadence: unchanged") for a in again.actions_taken)
    assert len(_rows()) == 1
    gone = skill.run({"table": "s.t", "identity": "a", "unschedule": True}, ctx)
    assert gone.ok and svc.JobStore(tmp_path).get("s.t") is None


def test_a_fire_for_a_table_with_no_job_does_nothing(tmp_path):
    ctx = SkillContext(registry=default_registry(), state_dir=tmp_path, logger=logging.getLogger("t"), user_prompt=None)
    r = adopt_tick.run({"table": "s.gone"}, ctx)
    assert r.ok and r.value["status"] == "idle"


def test_the_orchestrator_projects_the_jobs_when_it_starts(tmp_path, sqlite_store):
    from axiom.extensions.builtins.data_platform.orchestration.service import OrchestratorService
    from axiom.infra.skills import SkillRegistry

    svc.schedule(tmp_path, "s.t", params={})
    o = OrchestratorService(registry=SkillRegistry(), state_dir=tmp_path, clock=lambda: T0)
    assert o.ensure_adoption_cadences()["s.t"].startswith("registered")
    assert o.ensure_adoption_cadences()["s.t"].startswith("unchanged")


# -- change records -----------------------------------------------------------


def test_the_copy_is_bracketed_by_change_records(tmp_path):
    ledger = tmp_path / "changes.jsonl"
    svc._record("adoption copy of s.t started", "adopt s.t", "started", ledger=ledger)
    svc._record("done", "adopt s.t", "copied", ledger=ledger)
    lines = [json.loads(x) for x in ledger.read_text().splitlines()]
    kinds = [(x["phase"], x.get("kind"), x.get("outcome")) for x in lines]
    assert kinds == [("intent", "migration", None), ("outcome", None, "started"),
                     ("intent", "migration", None), ("outcome", None, "copied")]
    assert all(x["subject"] == "adopt s.t" for x in lines if x["phase"] == "intent")


def test_a_record_that_cannot_be_written_does_not_stop_the_copy(tmp_path):
    svc._record("x", "adopt s.t", "started", ledger=tmp_path)  # a directory: cannot be appended to
