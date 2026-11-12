# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Kit automations on a real Postgres: proven locally, promoted, run by the node.

The node is a kit medallion holding this site's rows and another site's rows.
PULSE's tables are an in-memory SQLite (the scheduler's own test seam), and
HERALD sends into an in-memory inbox, so nothing here reaches a developer's
database or anybody's inbox. What must hold:

- ``kit-try`` says exactly what one run would do, and sends and writes nothing;
- approval registers each automation in the kit registry and on PULSE, under an
  owner tied to the site's role; re-approving replaces them; retiring removes them;
- a fire (``fire_now`` + one engine tick, through the real skill executor) runs
  the verb as the site's role, sends one inbox message to the submitter, writes
  annotation rows the site's role can read back, records the run, and sees none
  of the other site's rows;
- the site's role cannot write another site's annotations;
- ``kit-automations`` through the data door is held to the key's site.

Needs Docker (or AXIOM_KIT_DSN); marked ``integration``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
from datetime import UTC, datetime

import pytest

from axiom.extensions.builtins.data_platform import kit
from axiom.extensions.builtins.data_platform.kit import automations as auto
from axiom.extensions.builtins.data_platform.kit import host
from axiom.extensions.builtins.data_platform.kit.bundle import pack
from axiom.extensions.builtins.data_platform.kit.medallion import connect, dsn

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (shutil.which("docker") or os.environ.get("AXIOM_KIT_DSN")),
        reason="needs Docker or AXIOM_KIT_DSN",
    ),
]

SITE, OTHER = "auto-loop", "auto-other"
SUBMITTER = "@contrib:auto-loop"

HOT = """description = "Days whose temperature rise ran above 0.1 degC."
every = "1h"
verb = "temperature_rise_by_day"
params = { since = "2026-09-01" }

[when]
column = "rise"
above = 0.1

[then]
notify = "submitter"
annotate = { label = "hot", note = "rise above 0.1" }
"""

#: Fires on whatever the verb returns, to show what the site's role can see.
SEEN = """description = "Which sites this kit can see, every day."
cron = "0 7 * * *"
verb = "sites_i_can_see"

[when]
any_rows = true

[then]
notify = "submitter"
"""

SITES_SQL = "SELECT site, count(*) AS n FROM silver.signals GROUP BY site\n"
SITES_VERB = """name = "sites_i_can_see"
description = "Every site whose rows this kit can read, with a row count."
object = "every_site"
sql = "SELECT site, n FROM {object} ORDER BY site"
"""


@pytest.fixture(scope="module")
def node(tmp_path_factory):
    repo = tmp_path_factory.mktemp("auto-node")
    kit.init(repo, SITE)
    p = kit.load(repo)
    kit.up(p)
    kit.try_kit(p)  # SITE's rows land in silver
    d = dsn(p)
    with connect(d) as conn:
        conn.execute(
            """INSERT INTO silver.signals (site, feed, channel, ts, value, unit, quality,
                   source_class, schema_ref, row_hash, role, derivation)
               SELECT %s, feed, channel, ts, value + 1000, unit, quality, source_class, schema_ref,
                      'other:' || row_hash, role, derivation
               FROM silver.signals WHERE site = %s ON CONFLICT DO NOTHING""",
            (OTHER, SITE),
        )
    host.ensure_registry(d)  # the operator's kit-host-setup
    yield p, d
    kit.down(p)


@pytest.fixture
def pulse():
    """PULSE on an in-memory SQLite, the scheduler's own test seam."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from axiom.extensions.builtins.schedule import hooks, store
    from axiom.extensions.builtins.schedule.db_models import Base

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
    hooks.clear()
    try:
        yield
    finally:
        hooks.clear()
        store.reset_provider()
        engine.dispose()


@pytest.fixture
def inbox(monkeypatch):
    """HERALD with only its inbox, held in memory."""
    from axiom.extensions.builtins.notifications.channels.inbox import InboxChannelAdapterProvider
    from axiom.extensions.builtins.notifications.inbox import InMemoryInboxStore
    from axiom.extensions.builtins.notifications.send import SendContext

    store = InMemoryInboxStore()

    def factory():
        ctx = SendContext(inbox_store=store)
        ctx.registry.register(InboxChannelAdapterProvider(store=store))
        return ctx

    monkeypatch.setattr(auto, "SEND_CONTEXT", factory)
    return store


def _contribution(tmp_path, site: str, files: dict[str, str]):
    repo = tmp_path / f"contrib-{site}"
    kit.init(repo, site)
    p = kit.load(repo)
    shutil.rmtree(p.root / "conform")
    (p.root / "gold" / "every_site.sql").write_text(SITES_SQL)
    (p.root / "gold" / "every_site.toml").write_text(
        'description = "Each site whose silver rows this kit can read, with a row count."\n')
    (p.root / "verbs" / "sites_i_can_see.toml").write_text(SITES_VERB)
    (p.root / "automations").mkdir(exist_ok=True)
    for name, text in files.items():
        (p.root / "automations" / f"{name}.toml").write_text(text)
    return kit.load(repo)


def _promote(d, project, *, principal=SUBMITTER, home=False):
    r = host.submit(d, pack(project).data, caller_site=project.tenant, principal=principal, home=home)
    assert r["state"] == "checked", r["problems"]
    return host.approve(d, r["id"], approver="@operator:home")


def _active():
    from axiom.extensions.builtins.schedule.api import list_schedules

    return [s for s in list_schedules("active") if s.name.startswith("kit automation")]


def _tick(monkeypatch, d):
    """One engine tick through the real skill executor, as the orchestrator runs it."""
    from axiom.extensions.builtins.data_platform import skills as data_skills
    from axiom.extensions.builtins.schedule import store
    from axiom.extensions.builtins.schedule.engine import EngineContext, tick
    from axiom.extensions.builtins.schedule.executor import SkillExecutor
    from axiom.extensions.builtins.schedule.lease import LeaseManager
    from axiom.infra.skills import SkillRegistry

    class Permit:
        def decide(self, envelope):
            return True

    monkeypatch.setenv("DP1_RAG_DSN", d)  # the node's own database
    registry = SkillRegistry()
    data_skills.bind(registry)
    ctx = EngineContext(session=store.session_scope, authz=Permit(), fire_log=store.SqlFireLog(),
                        executor=SkillExecutor(registry, logger=logging.getLogger("t")),
                        lease=LeaseManager(node_id="t", ttl_seconds=30), now_fn=lambda: datetime.now(UTC))
    return tick(ctx)


def test_kit_try_says_what_one_run_would_do_and_does_nothing(node, inbox, tmp_path):
    p, d = node
    (p.root / "automations").mkdir(exist_ok=True)
    (p.root / "automations" / "hot_days.toml").write_text(HOT)
    try:
        r = kit.try_kit(p)
    finally:
        (p.root / "automations" / "hot_days.toml").unlink()
    run = r["automations"]["hot_days"]
    assert run["matched"] > 0 and run["schedule"] == "every 1h"
    assert any(w.startswith("would notify the submitter: Kit auto-loop · hot_days") for w in run["would"])
    assert f"would annotate {run['matched']} row(s) 'hot'" in "\n".join(run["would"])
    assert inbox.all() == [], "kit-try sends nothing"
    with connect(d) as conn:
        assert conn.execute(f"SELECT count(*) FROM {p.gold_schema}.annotations").fetchone()[0] == 0


def test_approve_registers_reapprove_replaces_retire_removes(node, pulse, tmp_path):
    _, d = node
    first = _promote(d, _contribution(tmp_path, SITE, {"hot_days": HOT, "seen": SEEN}))
    assert first["automations"] == ["hot_days", "seen"]
    assert all(v.startswith("registered:") for v in first["scheduled"].values()), first["scheduled"]
    assert {(r["site"], r["name"], r["submitted_by"]) for r in auto.registered(d, site=SITE)} == {
        (SITE, "hot_days", SUBMITTER), (SITE, "seen", SUBMITTER)}

    from axiom.extensions.builtins.schedule import store
    from axiom.extensions.builtins.schedule.db_models import ScheduleDefinition

    with store.session_scope() as s:
        envs = [r.capability_envelope for r in s.query(ScheduleDefinition).filter_by(state="active")]
    assert {e["owner"] for e in envs} == {"@kit_auto_loop:auto-loop"}
    assert {e["role"] for e in envs} == {"kit_auto_loop"}
    assert {e["action"] for e in envs} == {auto.FIRE_ACTION}

    second = _promote(d, _contribution(tmp_path / "v2", SITE, {
        "hot_days": HOT.replace('every = "1h"', 'every = "30m"')}))
    assert second["scheduled"]["auto-loop/seen"] == "cancelled"
    assert second["scheduled"]["auto-loop/hot_days"].startswith("rescheduled:")
    (only,) = _active()
    assert only.cadence_kind == "interval"

    retired = host.retire(d, SITE, by="@operator:home")
    assert retired["unscheduled"] == {"auto-loop/hot_days": "cancelled"}
    assert _active() == [] and auto.registered(d, site=SITE) == []


def test_a_fire_notifies_and_annotates_as_the_site_and_sees_no_other_site(node, pulse, inbox,
                                                                            monkeypatch, tmp_path):
    from axiom.extensions.builtins.schedule import api

    _, d = node
    _promote(d, _contribution(tmp_path, SITE, {"hot_days": HOT, "seen": SEEN}))
    _promote(d, _contribution(tmp_path, OTHER, {}), principal="@operator:home", home=True)
    sched = {s.description.split()[2].split("/")[1].rstrip(":"): s for s in _active()}
    assert set(sched) == {"hot_days", "seen"}

    api.fire_now(sched["hot_days"].id)
    report = _tick(monkeypatch, d)
    assert report.fired == 1 and not report.errors, report

    (msg,) = inbox.all()
    assert msg.recipient == SUBMITTER
    text = msg.summary
    assert "Kit auto-loop · hot_days" in text and "rise above 0.1" in text and "kit-retire auto-loop" in text

    schema = "gold_auto_loop"
    with connect(d, role="kit_auto_loop") as conn:  # the site's role reads its own annotations back
        rows = conn.execute(f"SELECT label, note, automation, ts FROM {schema}.annotations").fetchall()
    (state,) = [r for r in auto.registered(d, site=SITE) if r["name"] == "hot_days"]
    assert rows and {(r[0], r[1], r[2]) for r in rows} == {("hot", "rise above 0.1", "hot_days")}
    assert state["last_outcome"] == f"annotated {len(rows)}, notified {SUBMITTER}"
    assert state["last_rows_matched"] == len(rows) and state["last_error"] is None
    assert all(r[3].year == 2026 for r in rows), "an annotation carries the row's own time"

    api.fire_now(sched["seen"].id)
    assert _tick(monkeypatch, d).fired == 1
    seen = inbox.all()[-1]
    assert f"site={SITE}" in seen.summary and OTHER not in seen.summary, "the verb ran as the site's role"

    with connect(d) as conn:
        can = conn.execute("SELECT has_table_privilege('kit_auto_loop', 'gold_auto_other.annotations', "
                           "'INSERT'), has_table_privilege('kit_auto_loop', 'gold_auto_other.annotations', "
                           "'SELECT'), has_schema_privilege('kit_auto_loop', 'gold_auto_loop', 'CREATE')"
                           ).fetchone()
    assert can == (False, False, False), "one site's role writes and reads only its own annotations"

    host.retire(d, OTHER, by="@operator:home")
    host.retire(d, SITE, by="@operator:home")
    assert _active() == []


def test_a_failed_fire_is_recorded_and_dead_lettered(node, pulse, inbox, monkeypatch, tmp_path):
    from axiom.extensions.builtins.schedule import api
    from axiom.extensions.builtins.schedule.api import status

    _, d = node
    _promote(d, _contribution(tmp_path, SITE, {"hot_days": HOT}))
    (s,) = _active()
    with connect(d) as conn:  # the verb's object is gone: the run must fail loudly
        conn.execute("DROP VIEW gold_auto_loop.daily_stats CASCADE")
    api.fire_now(s.id)
    assert _tick(monkeypatch, d).fired == 0
    assert status(s.id).last_outcome == "dead_letter"
    (state,) = auto.registered(d, site=SITE)
    assert state["last_outcome"] == "error" and "daily_stats" in state["last_error"]
    assert inbox.all() == []
    host.retire(d, SITE, by="@operator:home")


def test_kit_automations_through_the_door_is_held_to_the_keys_site(node, pulse, monkeypatch, tmp_path):
    from .test_a_kit_is_promoted_as_data import _door

    _, d = node
    monkeypatch.setenv("DP1_RAG_DSN", d)
    monkeypatch.setenv("AXIOM_SITE", "home-site")
    _promote(d, _contribution(tmp_path, SITE, {"hot_days": HOT}))
    mine = _door(SITE, SUBMITTER).post("/data/query", json={"verb": "data.kit_automations", "params": {}}).json()
    assert mine["ok"], mine
    (row,) = mine["value"]["automations"]
    assert (row["site"], row["name"], row["schedule"], row["scheduled"]) == (SITE, "hot_days", "every 1h", "active")
    assert row["when"] == "rise above 0.1" and row["last_run_at"] is None
    theirs = _door(OTHER, "@x:auto-other").post(
        "/data/query", json={"verb": "data.kit_automations", "params": {"site": SITE}}).json()
    assert theirs["ok"] is False and "reads site 'auto-other' only" in theirs["errors"][0], \
        "naming another site is refused, not answered as empty"
    listed = _door(OTHER, "@x:auto-other").post(
        "/data/query", json={"verb": "data.kit_automations", "params": {}}).json()
    assert listed["ok"] and listed["value"]["automations"] == []
    host.retire(d, SITE, by="@operator:home")
