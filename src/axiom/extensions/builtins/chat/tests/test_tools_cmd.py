# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``/tools``: every tool the agent can call, with permission, mutation and origin.

The listing is built from ``get_all_tools()`` (the table the chat actually offers the model) and
``agent.permissions`` (the state the approval gate consults), so it cannot describe a different set from
the one the model sees.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.chat import tools
from axiom.extensions.builtins.chat.builtin_commands import ensure_loaded
from axiom.extensions.builtins.chat.command_registry import REGISTRY
from axiom.infra.skills import SkillRegistry, SkillResult, SkillSpec

from ._tools_cmd_helpers import make_agent, run

_AGENT = ("cli", "mcp", "agent_tool")


@pytest.fixture(autouse=True)
def _no_installed_extension_scan(monkeypatch):
    """Scanning every installed extension's manifest takes seconds and is not what these tests are about.

    The scanners are real; only the roots they walk are emptied. ``test_manifest_*`` below points the real
    manifest scanner at a real manifest in a temp directory.
    """
    from axiom.extensions import discovery

    # The action ledger defaults to the platform database. A test must not write refusals into it.
    monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "jsonl")
    monkeypatch.setattr(tools, "_manifest_roots", lambda: [])
    monkeypatch.setattr(discovery, "discover_and_load_chat_tools", lambda *a, **k: [])


@pytest.fixture
def skills(monkeypatch):
    r = SkillRegistry()
    r.register_skill(SkillSpec(name="press.draft", fn=lambda p, c: SkillResult(ok=True),
                               description="Draft a document.", inputs={"source": "Path"},
                               surfaces=_AGENT))
    r.register_skill(SkillSpec(name="scan.status", fn=lambda p, c: SkillResult(ok=True),
                               description="Scan status.", side_effects=False, surfaces=_AGENT))
    monkeypatch.setattr(tools, "_skill_registry", lambda: r)

    def _ns(names):
        monkeypatch.setattr(tools, "_skill_namespaces", lambda: list(names))

    _ns([])
    return _ns


def _line(out: str, name: str) -> str:
    # A row starts with its list number (`/tools 3` opens it); the name follows.
    rows = [ln for ln in out.splitlines() if ln.strip().lstrip("0123456789 ").startswith(name + " ")]
    assert rows, f"{name} not listed in:\n{out}"
    return rows[0]


def test_registered_with_usage_and_summary():
    ensure_loaded()
    c = REGISTRY.get("/tools")
    assert c is not None and "tool" in c.summary.lower() and "filter" in c.usage


def test_lists_exactly_the_table_the_model_is_offered(skills):
    out = run("/tools")
    for name in tools.get_all_tools():
        assert name in out, f"{name} is offered to the model but not listed"


def test_builtin_write_tool_is_marked_as_mutating_and_read_tool_is_not(skills):
    out = run("/tools")
    assert "WRITE" in _line(out, "write_file")
    assert "read" in _line(out, "query_docs") and "WRITE" not in _line(out, "query_docs")


def test_default_permission_is_named_as_the_default_not_as_a_setting(skills):
    line = _line(run("/tools"), "write_file")
    assert "ask" in line and "default" in line


def test_override_is_shown_and_changes_what_happens_on_a_call(skills):
    agent = make_agent()
    agent.permissions.set("write_file", "deny")
    agent.permissions.set("doc_publish", "allow")
    out = run("/tools", agent)
    assert "deny" in _line(out, "write_file") and "default" not in _line(out, "write_file")
    assert "refused" in _line(out, "write_file")
    assert "allow" in _line(out, "doc_publish") and "runs" in _line(out, "doc_publish")


def test_a_write_tool_on_default_asks_and_a_read_tool_runs_unprompted(skills):
    out = run("/tools")
    assert "asks" in _line(out, "write_file")
    assert "runs" in _line(out, "query_docs")


def test_tools_are_grouped_by_where_they_come_from(skills):
    out = run("/tools")
    assert "Built-in" in out
    # agent_actions ships in chat/tools_ext, which is a different origin from the built-ins.
    assert "tools_ext" in out
    assert out.index("Built-in") < out.index("write_file")


def test_skill_tools_are_listed_under_their_namespace_when_enabled(skills):
    skills(["press"])
    out = run("/tools")
    assert "Skill: press" in out
    assert "press__draft" in out and "scan__status" not in out


def test_footer_says_skill_tools_are_off_and_how_to_turn_them_on(skills):
    out = run("/tools")
    assert "chat.tool_namespaces" in out and "settings set" in out
    assert "press" in out  # the namespaces that exist and declared agent_tool


def test_footer_does_not_claim_skills_are_off_when_a_namespace_is_set(skills):
    skills(["press"])
    assert "not offered" not in run("/tools")


def test_filter_narrows_by_name_and_origin(skills):
    out = run("/tools write")
    assert "write_file" in out and "query_docs" not in out


def test_filter_with_no_match_says_so_instead_of_printing_nothing(skills):
    assert "No tool matches" in run("/tools zzz_nothing")


def test_detail_shows_schema_description_permission(skills):
    agent = make_agent()
    agent.permissions.set("write_file", "allow")
    out = run("/tools write_file", agent)
    assert tools.get_all_tools()["write_file"].description.split(".")[0] in out
    assert "path" in out  # a parameter of write_file's schema
    assert "allow" in out and "Built-in" in out


def test_detail_says_never_called_rather_than_inventing_a_time(skills):
    out = run("/tools write_file")
    assert "not called in this session" in out


def test_detail_reports_a_call_from_this_session_with_its_age(skills):
    agent = make_agent()
    agent.session.add_message("tool", "{}", tool_calls=[{"tool_call_id": "1", "name": "write_file"}])
    out = run("/tools write_file", agent)
    assert "not called in this session" not in out
    assert "Last called" in out and "ago" in out or "just now" in out


def test_detail_reports_a_refusal_the_action_ledger_recorded(skills):
    from axiom.infra.authority import record_tool_refusal

    record_tool_refusal(tool_name="write_file", principal="@p", reason="site rule X")
    out = run("/tools write_file")
    assert "Last refused" in out and "site rule X" in out


def test_detail_for_a_skill_tool_names_its_namespace(skills):
    skills(["press"])
    out = run("/tools press__draft")
    assert "Skill: press" in out and "WRITE" in out


def test_get_all_tools_stamps_where_each_tool_came_from(skills):
    skills(["press"])
    table = tools.get_all_tools()
    assert table["write_file"].source == "builtin"
    assert table["agent_actions"].source == "tools_ext"
    assert table["press__draft"].source == "skill"


def test_manifest_declared_tools_are_labelled_as_such(monkeypatch, tmp_path, skills):
    root = tmp_path / "ext"
    root.mkdir()
    (root / "axiom-extension.toml").write_text(
        '[extension]\nname = "demo"\n\n[[extension.provides]]\nkind = "tool"\nname = "demo_probe"\n'
        'idempotent = true\ndescription = "Probe the demo."\n'
        'entry = "axiom.extensions.builtins.chat.tests._tools_cmd_helpers:make_agent"\n'
    )
    monkeypatch.setattr(tools, "_manifest_roots", lambda: [root])
    out = run("/tools demo")
    assert "Extension manifest" in out and "demo_probe" in out
    assert "read" in _line(out, "demo_probe")


def test_an_installed_extension_tool_is_labelled_as_such(monkeypatch, skills):
    from axiom.extensions import discovery
    from axiom.infra.orchestrator.actions import ActionCategory

    extra = tools.ToolDef(name="vendor_probe", description="d", category=ActionCategory.WRITE)
    monkeypatch.setattr(discovery, "discover_and_load_chat_tools", lambda *a, **k: [extra])
    out = run("/tools vendor")
    assert "Installed extension" in out and "WRITE" in _line(out, "vendor_probe")
