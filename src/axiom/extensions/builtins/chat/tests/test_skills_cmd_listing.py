# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``/skills``: the registry, grouped by namespace, with an honest "can the agent call it now".

The skills are real ``SkillSpec``s in a real ``SkillRegistry``; the chat's tool table is the real
``get_all_tools()`` reading that registry and the real ``chat.tool_namespaces`` seam.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.chat import tools
from axiom.extensions.builtins.chat.builtin_commands import ensure_loaded
from axiom.extensions.builtins.chat.command_registry import REGISTRY
from axiom.extensions.builtins.chat.commands_ext import skills_cmd
from axiom.infra.skills import SkillRegistry, SkillResult, SkillSpec

from ._sa_helpers import make_agent, run

_AGENT = ("cli", "mcp", "agent_tool")


def _ok(p, c):
    return SkillResult(ok=True)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    from axiom.extensions import discovery

    monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "jsonl")
    monkeypatch.setattr(tools, "_manifest_roots", lambda: [])
    monkeypatch.setattr(discovery, "discover_and_load_chat_tools", lambda *a, **k: [])


@pytest.fixture
def registry(monkeypatch):
    r = SkillRegistry()
    r.register_skill(SkillSpec(name="press.draft", fn=_ok, description="Draft a document.\nSecond line.",
                               inputs={"source": "Path!", "style": "str"}, surfaces=_AGENT))
    r.register_skill(SkillSpec(name="scan.status", fn=_ok, description="Scan status.", side_effects=False,
                               surfaces=_AGENT))
    r.register_skill(SkillSpec(name="hygiene.prune", fn=_ok, description="Prune things.",
                               side_effects=True, surfaces=("cli",)))  # never declared agent_tool
    r.register("legacy.thing", _ok)  # no SkillSpec
    r.register("legacy.other", _ok)
    monkeypatch.setattr(tools, "_skill_registry", lambda: r)
    monkeypatch.setattr(skills_cmd, "discover_catalog", lambda **k: SkillRegistry())
    monkeypatch.setattr(tools, "_skill_namespaces", lambda: [])
    return r


def _enable(monkeypatch, *names):
    monkeypatch.setattr(tools, "_skill_namespaces", lambda: list(names))


def _row(report, name):
    return next(r for r in report.rows if r.name == name)


def test_registered_with_usage_summary_and_completion():
    ensure_loaded()
    c = REGISTRY.get("/skills")
    assert c is not None and "skill" in c.summary.lower()
    assert "--missing" in c.usage and "--json" in c.usage and c.complete is not None


def test_callable_now_follows_the_namespace_setting(registry, monkeypatch):
    off = skills_cmd.list_skills()
    assert not _row(off, "press.draft").callable_now
    _enable(monkeypatch, "press")
    on = skills_cmd.list_skills()
    assert _row(on, "press.draft").callable_now
    assert not _row(on, "scan.status").callable_now  # a different namespace, still off


def test_callable_now_agrees_with_the_table_the_model_is_offered(registry, monkeypatch):
    _enable(monkeypatch, "press", "hygiene")
    offered = set(tools.get_all_tools())
    for row in skills_cmd.list_skills().rows:
        assert row.callable_now == (row.tool_name in offered), row.name


def test_a_disabled_namespace_gets_the_exact_setting_command(registry, monkeypatch):
    out = run("/skills")
    assert "axi settings set chat.tool_namespaces press" in out
    _enable(monkeypatch, "scan")
    # enabling another namespace must keep the ones already enabled
    assert "chat.tool_namespaces scan,press" in run("/skills")


def test_a_skill_that_never_declared_agent_tool_is_not_offered_a_setting_that_cannot_work(registry, monkeypatch):
    _enable(monkeypatch, "hygiene")
    row = _row(skills_cmd.list_skills(), "hygiene.prune")
    assert not row.callable_now and row.enable_command is None
    assert "agent_tool" in row.reason


def test_skills_with_no_spec_are_counted_and_named_only_on_request(registry):
    out = run("/skills")
    assert "2 registered skills have NO SkillSpec" in out
    assert "legacy.thing" not in out  # names are behind --missing
    missing = run("/skills --missing")
    assert "legacy.thing" in missing and "legacy.other" in missing and "press.draft" not in missing


def test_the_count_does_not_pretend_when_everything_has_a_spec(monkeypatch):
    r = SkillRegistry()
    r.register_skill(SkillSpec(name="a.b", fn=_ok, description="x", surfaces=_AGENT))
    monkeypatch.setattr(tools, "_skill_registry", lambda: r)
    monkeypatch.setattr(skills_cmd, "discover_catalog", lambda **k: SkillRegistry())
    assert "Every registered skill has a SkillSpec" in run("/skills --missing")


def test_grouped_by_namespace_with_kind_and_description(registry):
    out = run("/skills")
    for ns in ("press", "scan", "hygiene"):
        assert f"  {ns}  (" in out
    assert "Draft a document." in out and "Second line." not in out  # one line each
    row = next(ln for ln in out.splitlines() if ln.strip().lstrip("0123456789 ").startswith("scan.status"))
    assert "read" in row
    assert "WRITE" in next(ln for ln in out.splitlines() if ln.strip().lstrip("0123456789 ").startswith("hygiene.prune"))
    # side_effects never declared: said so, not shown as read-only
    assert "undeclared" in next(ln for ln in out.splitlines() if ln.strip().lstrip("0123456789 ").startswith("press.draft"))


def test_filter_matches_name_namespace_and_description(registry):
    out = run("/skills scan")
    assert "scan.status" in out and "press.draft" not in out
    assert "press.draft" in run("/skills document")
    assert "No skill matches" in run("/skills zzz-nothing")


def test_json_is_machine_readable_and_complete(registry, monkeypatch):
    _enable(monkeypatch, "press")
    data = json.loads(run("/skills --json"))
    assert data["total"] == 5 and data["callable_count"] == 1
    assert sorted(data["missing"]) == ["legacy.other", "legacy.thing"]
    names = {r["name"]: r for r in data["rows"]}
    assert names["press.draft"]["callable_now"] is True
    assert names["scan.status"]["enable_command"].endswith("chat.tool_namespaces press,scan")


def test_completion_offers_skill_names_namespaces_and_flags(registry):
    cands = dict(skills_cmd.complete_skills(make_agent(), []))
    assert {"press.draft", "press", "scan", "--missing", "--json"} <= set(cands)
    assert cands["press.draft"] == "Draft a document."
    assert skills_cmd.complete_skills(make_agent(), ["press"]) == []


def test_the_chat_registry_missing_a_catalog_skill_says_so(monkeypatch):
    chat = SkillRegistry()
    catalog = SkillRegistry()
    catalog.register_skill(SkillSpec(name="data.q", fn=_ok, description="q", surfaces=_AGENT))
    monkeypatch.setattr(tools, "_skill_registry", lambda: chat)
    monkeypatch.setattr(skills_cmd, "discover_catalog", lambda **k: catalog)
    monkeypatch.setattr(tools, "_skill_namespaces", lambda: ["data"])
    row = _row(skills_cmd.list_skills(), "data.q")
    assert not row.callable_now and "registry" in row.reason


def test_discover_catalog_binds_real_extension_skills():
    """The real catalog: it must find specced skills in the installed extensions (no stub registry)."""
    cat = skills_cmd.discover_catalog(refresh=True)
    assert cat.specs(), "no extension bound any SkillSpec"
