# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The data-growth duty across the crew, through the registry.

PLINTH declares, KEEL measures, judges, explains and (only once approved) applies, PULSE
schedules, HERALD alerts once per cause, CLERK tracks each proposal until it is done. The
configured model words KEEL's output and nothing else; without one the built-in wording is
used and says so.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

from axiom.extensions.builtins.install_health.skills import doctor
from axiom.extensions.builtins.node import data_growth as dg
from axiom.extensions.builtins.node import proposals
from axiom.extensions.builtins.node.skills import bind
from axiom.extensions.builtins.node.skills import data_growth as growth
from axiom.extensions.builtins.node.skills import data_retention as retention
from axiom.infra import human_layer
from axiom.infra.skill_chain import plan_digest
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec

GB = dg.GB
BUILTINS = Path(dg.__file__).resolve().parents[1]


class Catalog:
    def tables(self):
        return [
            dg.TableStat("store", "readings", total_bytes=201 * GB, table_bytes=86 * GB,
                         index_bytes=115 * GB, live_rows=290_000_000, inserts=290_000_000),
            dg.TableStat("store", "events", total_bytes=20 * GB, table_bytes=15 * GB,
                         index_bytes=5 * GB, hypertable=True, compression=True,
                         compress_after="7d", chunk_bytes_per_day=0.2 * GB),
            dg.TableStat("store", "ledger", total_bytes=9 * GB, table_bytes=6 * GB,
                         index_bytes=3 * GB, live_rows=21_800_000, inserts=21_800_000,
                         updates=4_100_000_000),
        ]

    def indexes(self):
        return []

    def database_bytes(self):
        return 325 * GB

    def data_directory(self):
        return "/data/pg"

    def stats_reset(self):
        return None


def _usage(path):
    return SimpleNamespace(total=2000 * GB, used=700 * GB, free=1300 * GB)


class FakeModel:
    def __init__(self, text="In plain words: one table grows without a bound.", fail=False):
        self.text, self.fail, self.prompts = text, fail, []

    def complete(self, prompt, system="", task="", max_tokens=0):
        self.prompts.append((system, prompt, task))
        if self.fail:
            raise RuntimeError("provider down")
        return SimpleNamespace(text=self.text, success=True, provider="fake")


class Crew:
    """A registry holding KEEL plus fakes for PLINTH, HERALD and CLERK."""

    def __init__(self, tmp_path, rules=None, gaps=None, backup_gaps=None):
        self.alerts, self.followups, self.done, self.marks = [], [], [], []
        self.reg = SkillRegistry()
        bind(self.reg)
        self._fake("data.storage_rules", lambda p: {
            "rules": rules or [], "gaps": (gaps or []) + (backup_gaps or []),
            "storage_gaps": gaps or [], "backup_gaps": backup_gaps or [], "backup": {}})
        self._fake("program.followup_mark", lambda p: self.marks.append(p) or {}, write=True)
        self._fake("notifications.alert", lambda p: self.alerts.append(p) or {}, write=True)
        self._fake("program.followup_add",
                   lambda p: self.followups.append(p) or {"id": f"fu-{len(self.followups)}"},
                   write=True)
        self._fake("program.followup_done", lambda p: self.done.append(p) or {}, write=True)
        self.ctx = SkillContext(registry=self.reg, state_dir=tmp_path,
                                logger=logging.getLogger("t"))

    def _fake(self, name, fn, write=False):
        self.reg.register_skill(
            SkillSpec(name=name, fn=lambda p, c: SkillResult(ok=True, value=fn(p)),
                      description=name, surfaces=("cli",),
                      risk="reversible-write" if write else "read", side_effects=write),
            mutating=write,
        )

    def growth(self, **kw):
        return growth.run({"_catalog": Catalog(), "_disk_usage": _usage, **kw}, self.ctx)


# -- the shared human layer --------------------------------------------------------------


def test_the_human_layer_words_facts_through_the_model():
    m = FakeModel()
    out = human_layer.explain({"n": 1}, agent="KEEL", ask="say it", fallback="built in",
                              gateway=m)
    assert out.text == m.text and out.by == "model:fake" and out.from_model and not out.note
    system, prompt, task = m.prompts[0]
    assert "KEEL" in system and '"n": 1' in prompt and task == human_layer.TASK


def test_the_human_layer_falls_back_and_says_so():
    none = human_layer.explain({}, agent="KEEL", ask="x", fallback="built in", gateway=None)
    failed = human_layer.explain({}, agent="KEEL", ask="x", fallback="built in",
                                 gateway=FakeModel(fail=True))
    empty = human_layer.explain({}, agent="KEEL", ask="x", fallback="built in",
                                gateway=FakeModel(text="  "))
    for out in (none, failed, empty):
        assert out.text == "built in" and out.by == "deterministic"
        assert out.note.startswith("model unavailable")


# -- KEEL: explanation, why, alerts ---------------------------------------------------------


def test_keel_explains_with_the_model_and_the_decision_stays_deterministic(tmp_path):
    crew = Crew(tmp_path)
    with_model = crew.growth(_gateway=FakeModel())
    without = Crew(tmp_path / "b").growth(_gateway=None)
    assert with_model.value["explanation"]["by"] == "model:fake"
    assert without.value["explanation"]["by"] == "deterministic"
    assert "model unavailable" in without.value["explanation"]["note"]
    assert "Growing fastest" in without.value["explanation"]["text"]  # the why, built in
    assert [f["code"] for f in with_model.value["findings"]] == [
        f["code"] for f in without.value["findings"]]


def test_alert_text_is_worded_by_the_model_or_falls_back(tmp_path):
    crew = Crew(tmp_path)
    crew.growth(alert=True, _gateway=FakeModel(text="Plain alert."))
    assert crew.alerts and all(a["summary"] == "Plain alert." for a in crew.alerts)
    crew2 = Crew(tmp_path / "b")
    crew2.growth(alert=True, _gateway=FakeModel(fail=True))
    assert all(a["summary"].startswith("data growth") for a in crew2.alerts)


# -- CLERK: one follow-up per proposal, closed when it is applied ---------------------------


def test_a_proposal_files_one_followup_and_is_not_realerted_daily(tmp_path):
    crew = Crew(tmp_path)
    first = crew.growth(alert=True, _gateway=None)
    n_followups, n_alerts = len(crew.followups), len(crew.alerts)
    assert n_followups == len([f for f in first.value["findings"]
                               if f["severity"] in ("warning", "critical")])
    assert n_alerts == 2  # unbounded growth, update churn: one per cause
    crew.growth(alert=True, _gateway=None)  # the next day, nothing fixed yet
    assert len(crew.followups) == n_followups and len(crew.alerts) == n_alerts


def test_applying_an_approved_plan_closes_its_followup_with_what_ran(tmp_path):
    crew = Crew(tmp_path)
    rep = crew.growth(alert=True, _gateway=None)
    churn = next(f for f in rep.value["findings"] if f["code"] == "update-churn")
    fix = churn["fix"]
    assert fix["capability"] == "node.data_apply"
    assert fix["plan_hash"] == plan_digest("node.data_apply", fix["params"])
    ran: list[str] = []
    conn = SimpleNamespace(execute=ran.append)
    res = retention.apply({**fix["params"], "_conn": conn}, crew.ctx)
    assert res.ok and ran == fix["sql"]
    assert len(crew.done) == 1 and crew.done[0]["id"].startswith("fu-")
    assert "applied to store.ledger" in crew.done[0]["note"]
    ledger = proposals.Ledger.under(tmp_path)
    assert ledger.get(fix["plan_hash"])["state"] == "applied"


def test_apply_runs_only_allowlisted_statements(tmp_path):
    crew = Crew(tmp_path)
    ran: list[str] = []
    res = retention.apply({"table": "store.x", "sql": ["DELETE FROM store.x"],
                           "_conn": SimpleNamespace(execute=ran.append)}, crew.ctx)
    assert not res.ok and not ran


def test_apply_is_destructive_and_gated_by_an_approved_plan(tmp_path):
    from axiom.infra.skill_dispatch import invoke_capability

    crew = Crew(tmp_path)
    assert crew.reg.spec("node.data_apply").effective_risk == "destructive"
    res = invoke_capability(crew.reg, "node.data_apply",
                            {"table": "store.ledger",
                             "sql": ["ALTER TABLE store.ledger SET (fillfactor = 80)"]},
                            crew.ctx, surface="cli")
    assert not res.ok  # no approved plan_hash: the gate opens a plan instead of running


# -- KEEL retention for tables that declare a bound -----------------------------------------


def test_keel_plans_retention_only_where_a_bound_is_declared(tmp_path):
    crew = Crew(tmp_path, rules=[
        {"table": "store.events", "retain": "400d", "max_rows": None},
        {"table": "store.ledger", "retain": None, "max_rows": 1_000_000},
        {"table": "store.settings", "retain": None, "max_rows": None, "grows": False},
    ])
    res = retention.retention({"_catalog": Catalog(), "_disk_usage": _usage, "_gateway": None},
                              crew.ctx)
    assert res.ok
    by = {p["table"]: p for p in res.value["plans"]}
    assert set(by) == {"store.events", "store.ledger"}
    assert by["store.events"]["fix"]["sql"] == [
        "SELECT add_retention_policy('store.events', INTERVAL '400d')"]
    assert dg.allowed(by["store.events"]["fix"]["sql"][0])
    assert by["store.ledger"]["state"] == "over its limit"


# -- install: the safeguards said loudly --------------------------------------------------


GAP = "store.daily is partitioned by time and declares no retain_periods"


def test_install_doctor_refuses_on_a_storage_gap_and_warns_on_an_unproven_backup(tmp_path):
    crew = Crew(tmp_path, gaps=[GAP], backup_gaps=["backups are on but never restored"])
    checks = {c["name"]: c for c in doctor.data_safeguard_checks(crew.ctx)}
    assert checks["Data storage rules"]["refuses_install"] is True
    assert checks["Data storage rules"]["status"].startswith("REFUSED AT INSTALL")
    assert checks["Backup restore check"]["status"].startswith("WARNING")
    assert not checks["Backup restore check"].get("refuses_install")
    assert checks["Data growth check"]["ok"] is True


class FakeUpdater:
    def __init__(self):
        self.ran = False

    def update_all(self, pull=False):
        self.ran = True
        return []


def test_install_refuses_a_storage_gap_without_the_override(tmp_path):
    from axiom.extensions.builtins.install_health.skills import update_apply

    crew = Crew(tmp_path, gaps=[GAP])
    up = FakeUpdater()
    res = update_apply.run({"_updater": up}, crew.ctx)
    assert not res.ok and not up.ran
    assert "allow_undeclared_storage" in res.errors[0] and "store.daily" in res.errors[0]


def test_the_override_installs_and_is_recorded(tmp_path, monkeypatch):
    from axiom.extensions.builtins.install_health.skills import update_apply, update_check

    monkeypatch.setattr(update_check, "results_to_value",
                        lambda results, applied: {"failed": False, "steps": [], "text": "ok"})
    crew = Crew(tmp_path, gaps=[GAP])
    up = FakeUpdater()
    res = update_apply.run({"_updater": up, "allow_undeclared_storage": "adopting next week"},
                           crew.ctx)
    assert res.ok and up.ran
    assert res.value["storage_override"]["reason"] == "adopting next week"
    rec = (tmp_path / doctor.OVERRIDES_FILE).read_text()
    assert "adopting next week" in rec and "store.daily" in rec


def test_an_unproven_backup_does_not_refuse_the_install(tmp_path, monkeypatch):
    from axiom.extensions.builtins.install_health.skills import update_apply, update_check

    monkeypatch.setattr(update_check, "results_to_value",
                        lambda results, applied: {"failed": False, "steps": [], "text": "ok"})
    crew = Crew(tmp_path, backup_gaps=["backups are on but never restored"])
    res = update_apply.run({"_updater": FakeUpdater()}, crew.ctx)
    assert res.ok and "WARNING Backup restore check" in res.value["text"]


# -- approval, and CLERK's digest ------------------------------------------------------------


def test_approving_a_plan_marks_its_followup_approved_once(tmp_path):
    crew = Crew(tmp_path)
    rep = crew.growth(alert=True, _gateway=None)
    churn = next(f for f in rep.value["findings"] if f["code"] == "update-churn")["fix"]

    def find_plan(ctx, capability, plan_hash):
        ok = capability == churn["capability"] and plan_hash == churn["plan_hash"]
        return SimpleNamespace(status=SimpleNamespace(value="approved")) if ok else None

    open_now = growth.open_proposals({"_find_plan": find_plan}, crew.ctx)
    assert open_now.value["approved_now"] == [churn["plan_hash"]]
    assert len(crew.marks) == 1 and crew.marks[0]["mark"] == "approved"
    states = {p["plan_hash"]: p["state"] for p in open_now.value["proposals"]}
    assert states[churn["plan_hash"]] == "approved"
    crew.growth(alert=True, _gateway=None, _find_plan=find_plan)  # next day: no second mark
    assert len(crew.marks) == 1
    retention.apply({**churn["params"], "_conn": SimpleNamespace(execute=lambda s: None)}, crew.ctx)
    after = growth.open_proposals({"_find_plan": find_plan}, crew.ctx)
    assert churn["plan_hash"] not in {p["plan_hash"] for p in after.value["proposals"]}


def test_proposing_opens_the_plan_on_the_approval_queue(tmp_path):
    from axiom.infra.skill_chain import find_plan

    crew = Crew(tmp_path)
    rep = crew.growth(alert=True, _gateway=None)
    churn = next(f for f in rep.value["findings"] if f["code"] == "update-churn")["fix"]
    action = find_plan(crew.ctx, churn["capability"], churn["plan_hash"])
    assert action is not None and action.status.value == "pending"


def test_clerks_weekly_digest_lists_open_data_growth_findings(tmp_path):
    from axiom.extensions.builtins.program.skills import consolidate

    crew = Crew(tmp_path)
    crew.growth(alert=True, _gateway=None)
    found = consolidate._open_data_growth(crew.ctx)
    assert {g["rule"] for g in found} >= {"unbounded-growth", "update-churn"}
    subject, body = consolidate.render_message({
        "program": {"name": "p"}, "by_kind": {}, "count": 0, "generated_at": "now",
        "proposals": [], "data_growth": found})
    assert "Open data-growth findings" in body and "store.readings" in body


def test_install_doctor_says_when_the_growth_check_has_nobody_to_alert(tmp_path):
    reg = SkillRegistry()
    reg.register_skill(SkillSpec(name="data.storage_rules",
                                 fn=lambda p, c: SkillResult(ok=True, value={"gaps": []}),
                                 description="r", surfaces=("cli",), risk="read",
                                 side_effects=False))
    ctx = SkillContext(registry=reg, state_dir=tmp_path, logger=logging.getLogger("t"))
    checks = {c["name"]: c for c in doctor.data_safeguard_checks(ctx)}
    assert checks["Data growth check"]["ok"] is False
    assert "node.data_growth" in checks["Data growth check"]["status"]


# -- roles ---------------------------------------------------------------------------------


def test_each_agent_in_the_chain_knows_its_part():
    personas = {
        "PLINTH": "data_platform/agents/plinth/persona.md",
        "KEEL": "node/agents/keel/persona.md",
        "PULSE": "schedule/agents/pulse/persona.md",
        "HERALD": "notifications/agents/herald/persona.md",
    }
    for agent, rel in personas.items():
        text = (BUILTINS / rel).read_text()
        assert "## Data growth and storage safeguards" in text, agent
        assert f"**{agent}'s part:**" in text, agent
    for folded in ("hygiene/agents/tidy/persona.md", "diagnostics/agents/triage/persona.md"):
        assert "Data growth" not in (BUILTINS / folded).read_text()


def test_keel_reaches_other_agents_only_through_the_registry():
    for rel in ("node/data_growth.py", "node/proposals.py", "node/skills/data_growth.py",
                "node/skills/data_retention.py"):
        text = (BUILTINS / rel).read_text()
        for other in ("builtins.data_platform", "builtins.program", "builtins.notifications"):
            assert other not in text, (rel, other)


def test_the_digest_is_news_when_only_the_data_growth_list_changes():
    from axiom.extensions.builtins.program.skills import consolidate

    base = "abc123def456"
    one = [{"plan_hash": "h1", "state": "open"}]
    assert consolidate.with_data_growth(base, []) == base
    a = consolidate.with_data_growth(base, one)
    assert a != base
    assert consolidate.with_data_growth(base, [{"plan_hash": "h1", "state": "approved"}]) != a
    assert consolidate.with_data_growth(base, one + [{"plan_hash": "h2", "state": "open"}]) != a
    assert consolidate.with_data_growth(base, list(one)) == a  # unchanged: not re-sent


def test_axi_update_has_the_storage_override_flag_and_records_it(tmp_path):
    from axiom.extensions.builtins.update import cli as update_cli

    args = update_cli.build_parser().parse_args(["--allow-undeclared-storage", "adopting soon"])
    assert args.allow_undeclared_storage == "adopting soon"
    crew = Crew(tmp_path, gaps=[GAP])
    go, msg = update_cli._storage_gate("", crew.ctx)
    assert not go and "allow_undeclared_storage" in msg
    go, msg = update_cli._storage_gate("adopting soon", crew.ctx)
    assert go and "recorded" in msg
    assert "adopting soon" in (tmp_path / doctor.OVERRIDES_FILE).read_text()


def test_axi_update_refuses_before_installing_on_a_storage_gap(monkeypatch):
    from axiom.extensions.builtins.update import cli as update_cli

    ran: list[bool] = []
    monkeypatch.setattr(update_cli, "_storage_gate", lambda reason, ctx=None: (False, "refused"))
    monkeypatch.setattr(update_cli.Updater, "update_all", lambda self, pull=False: ran.append(True))
    assert update_cli.main([]) == 1 and not ran
