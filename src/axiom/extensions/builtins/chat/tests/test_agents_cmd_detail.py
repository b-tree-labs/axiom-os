# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``/agents``: enabled state, detail view, held actions, RACI posture, the delegation notice, JSON.

Real manifests, the real settings store, the real consent record, the real approval queue file.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions import discovery
from axiom.extensions.builtins.agents.consent import AgentConsent
from axiom.extensions.builtins.chat.commands_ext import agents_cmd
from axiom.infra.orchestrator.actions import Action
from axiom.infra.orchestrator.approval_store import FileActionStore
from axiom.infra.skills import SkillRegistry, SkillResult, SkillSpec

from ._agents_world import discover, make_ext
from ._sa_helpers import run

NOW = 1_800_000_000.0


@pytest.fixture
def world(tmp_path, monkeypatch):
    exts = tmp_path / "exts"
    exts.mkdir()
    make_ext(exts, "alpha", interval=300)
    make_ext(exts, "beta", scheduled=False)
    found = discover(exts)
    monkeypatch.setattr(discovery, "discover_extensions", lambda *a, **k: found)
    monkeypatch.setattr(discovery, "surfaced_extensions", lambda *a, **k: found)
    monkeypatch.setattr(agents_cmd, "_agents_dir", lambda: tmp_path / "agents")
    from axiom.extensions.builtins.chat.commands_ext import skills_cmd

    monkeypatch.setattr(skills_cmd, "discover_catalog", lambda **k: SkillRegistry())
    return found, tmp_path / "agents"


def _row(report, name):
    return next(r for r in report.rows if r.name == name)


def test_autonomy_off_is_stated_per_agent_and_nothing_claims_to_run(world):
    found, agents = world
    report = agents_cmd.list_agents(extensions=found, agents_dir=agents, autonomy=False, consent=None)
    assert _row(report, "alpha").runs.startswith("OFF: autonomy.enabled is false")
    assert "axi settings set autonomy.enabled true" in _row(report, "alpha").runs
    assert _row(report, "beta").runs.startswith("on demand")
    assert "autonomy: OFF" in agents_cmd.format_listing(report)


def test_autonomy_on_and_consent_gate_dispatch(world):
    found, agents = world
    on = agents_cmd.list_agents(extensions=found, agents_dir=agents, autonomy=True, consent=None)
    assert _row(on, "alpha").runs == "scheduled"
    only_others = AgentConsent(decided=True, enabled=["somebody-else"])
    off = agents_cmd.list_agents(extensions=found, agents_dir=agents, autonomy=True, consent=only_others)
    assert _row(off, "alpha").consent == "not enabled by operator"
    assert _row(off, "alpha").runs.startswith("OFF: not in the operator's enabled agents")
    allowed = AgentConsent(decided=True, enabled=["alpha"])
    yes = agents_cmd.list_agents(extensions=found, agents_dir=agents, autonomy=True, consent=allowed)
    assert _row(yes, "alpha").runs == "scheduled" and _row(yes, "alpha").consent == "enabled"
    out = AgentConsent(decided=True, opted_out=True)
    none = agents_cmd.list_agents(extensions=found, agents_dir=agents, autonomy=True, consent=out)
    assert _row(none, "alpha").runs.startswith("OFF: operator opted out")


def test_autonomy_is_read_from_the_real_setting_default_off(world, tmp_path, monkeypatch):
    from axiom.extensions.builtins.settings import store

    monkeypatch.setattr(store, "_PROJECT_SETTINGS_PATH", tmp_path / "settings.toml")
    found, agents = world
    assert agents_cmd.list_agents(extensions=found, agents_dir=agents, consent=None).autonomy_on is False
    store.SettingsStore().set("autonomy.enabled", True)
    assert agents_cmd.list_agents(extensions=found, agents_dir=agents, consent=None).autonomy_on is True


def test_consent_is_read_from_the_real_record(world):
    from axiom.extensions.builtins.agents import consent as consent_mod

    consent_mod.consent_path().write_text(json.dumps({"decided": True, "enabled": ["beta"]}))
    found, agents = world
    report = agents_cmd.list_agents(extensions=found, agents_dir=agents, autonomy=True)
    assert _row(report, "alpha").consent == "not enabled by operator"


def test_listing_names_purpose_owner_and_the_delegation_gap(world):
    found, agents = world
    out = agents_cmd.format_listing(agents_cmd.list_agents(extensions=found, agents_dir=agents, autonomy=False, consent=None))
    assert "alpha does one job." in out and "alpha" in out and "beta answers when asked." in out
    assert "NOT available yet" in out and "#1067" in out
    assert "/delegate" not in out and "/dispatch" not in out  # never imply a command that does not exist


def test_detail_shows_persona_summary_and_declared_tier(world):
    found, agents = world
    d = agents_cmd.describe_agent("alpha", extensions=found, agents_dir=agents, autonomy=False, consent=None,
                                  catalog=SkillRegistry())
    assert "ALPHA - The Tester" in d.persona_summary and "checks things." in d.persona_summary
    out = agents_cmd.format_detail(d, agents_cmd.list_agents(extensions=found, agents_dir=agents, autonomy=False, consent=None))
    assert "declared default_consent: core" in out
    assert "no RACI ledger recorded for this agent" in out
    assert "#1067" in out


def test_a_missing_persona_file_is_said_not_invented(world, tmp_path):
    exts = tmp_path / "exts2"
    exts.mkdir()
    make_ext(exts, "nop", persona=None)
    found = discover(exts)
    d = agents_cmd.describe_agent("nop", extensions=found, agents_dir=tmp_path / "a", autonomy=False, consent=None,
                                  catalog=SkillRegistry())
    assert "not readable" in d.persona_summary


def test_detail_lists_only_the_skills_its_extension_owns(world, monkeypatch):
    found, agents = world

    def ours(p, c):
        return SkillResult(ok=True)

    ours.__module__ = "axiom.extensions.builtins.hygiene.skills.fake"
    reg = SkillRegistry()
    reg.register_skill(SkillSpec(name="hygiene.fake", fn=ours, description="d", allowed_tools=("read_file",)))
    from axiom.extensions.discovery import _extension_source_package

    class _Ext:  # a real path under builtins/, so the package derivation is the real one
        name = "hygiene"
        root = __import__("pathlib").Path(discovery.__file__).parent / "builtins" / "hygiene"

    assert _extension_source_package(_Ext) == "axiom"
    names, allowed = agents_cmd._owned_skills(_Ext, reg)
    assert names == ["hygiene.fake"] and allowed == ["read_file"]
    other = SkillRegistry()
    foreign = lambda p, c: SkillResult(ok=True)  # noqa: E731
    foreign.__module__ = "axiom.extensions.builtins.press.skills.x"
    other.register_skill(SkillSpec(name="press.x", fn=foreign, description="d"))
    assert agents_cmd._owned_skills(_Ext, other) == ([], [])


def test_held_actions_are_attributed_by_owning_skill_and_others_are_not(tmp_path):
    q = tmp_path / "approvals.json"
    store = FileActionStore(q)
    store.put(Action(name="hygiene.reclaim", params={}))
    store.put(Action(name="press.draft", params={}))
    held, note = agents_cmd._held_for(["hygiene.reclaim", "hygiene.ls"], q)
    assert [h.name for h in held] == ["hygiene.reclaim"] and note == ""


def test_an_empty_queue_says_none_and_a_broken_one_says_unknown(tmp_path):
    q = tmp_path / "approvals.json"
    held, note = agents_cmd._held_for(["hygiene.ls"], q)
    assert held == () and note == ""
    q.write_text("{ not json")
    held, note = agents_cmd._held_for(["hygiene.ls"], q)
    assert held == () and note.startswith("unknown")


def test_an_agent_that_owns_no_skills_cannot_be_credited_with_an_empty_queue(tmp_path):
    held, note = agents_cmd._held_for([], tmp_path / "approvals.json")
    assert held == () and "none attributable" in note


def test_raci_ledger_states_are_shown_when_the_ledger_exists(world):
    from axiom.agents.raci import RACILedger

    found, agents = world
    ledger = RACILedger()
    ledger.record_yes("ingest local repo updates")
    ledger.record_no("diagnose ci failures")
    ledger.save(agents / "alpha" / "raci_state.json")
    d = agents_cmd.describe_agent("alpha", extensions=found, agents_dir=agents, autonomy=False, consent=None,
                                  catalog=SkillRegistry())
    out = agents_cmd.format_detail(d, agents_cmd.list_agents(extensions=found, agents_dir=agents, autonomy=False, consent=None))
    assert "ingest local repo updates: approved" in out and "diagnose ci failures: backing_off" in out


def test_command_filter_detail_json_and_unknown(world):
    found, agents = world
    listing = run("/agents")
    assert "alpha" in listing and "beta" in listing
    assert "beta answers" not in run("/agents alpha")  # an exact name opens the detail, not the table
    assert "beta answers" not in run("/agents does one")  # a filter narrows the table
    assert "No agent matches" in run("/agents nope")
    detail = run("/agents alpha")
    assert "Persona" in detail and "Held actions" in detail
    data = json.loads(run("/agents --json"))
    assert data["delegation_available"] is False and {r["name"] for r in data["rows"]} == {"alpha", "beta"}
    one = json.loads(run("/agents alpha --json"))
    assert one["row"]["name"] == "alpha" and "persona_summary" in one


def test_completion_offers_agent_names(world):
    from ._sa_helpers import make_agent

    got = dict(agents_cmd.complete_agents(make_agent(), []))
    assert "alpha" in got and "--json" in got and agents_cmd.complete_agents(make_agent(), ["alpha"]) == []
