# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The chat loop exposes registered capabilities behind a scoped namespace list.

Chat = CLI: a capability the CLI can run is callable from chat with the same
name-mangling, schema and approval category (one projector), and it answers
in the same shape MCP dispatch returns. Exposure is bounded: nothing is
exposed until ``chat.tool_namespaces`` names a namespace.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.chat import tools
from axiom.extensions.builtins.chat.tools import ToolDef, execute_tool, get_all_tools
from axiom.infra.orchestrator.actions import ActionCategory
from axiom.infra.skills import SkillRegistry, SkillResult, SkillSpec


@pytest.fixture
def registry():
    seen: list[dict] = []

    def _draft(params, ctx):
        seen.append(dict(params))
        return SkillResult(
            ok=True, value={"rendered": params.get("source")}, actions_taken=["drafted"]
        )

    def _publish(params, ctx):
        raise RuntimeError("upstream down")

    def _status(params, ctx):
        return SkillResult(ok=True, value={"count": 3})

    r = SkillRegistry()
    r.register_skill(
        SkillSpec(name="press.draft", fn=_draft, description="Draft.", inputs={"source": "Path"})
    )
    r.register_skill(
        SkillSpec(
            name="press.publish", fn=_publish, description="Publish.", inputs={"source": "Path"}
        )
    )
    r.register_skill(
        SkillSpec(name="scan.status", fn=_status, description="Status.", side_effects=False)
    )
    r.seen = seen  # type: ignore[attr-defined]
    return r


@pytest.fixture
def scoped(monkeypatch, registry):
    """Registry injected; namespaces set per test via the returned setter."""
    monkeypatch.setattr(tools, "_skill_registry", lambda: registry)

    def _set(namespaces):
        monkeypatch.setattr(tools, "_skill_namespaces", lambda: list(namespaces))

    _set([])
    return _set


def test_no_namespaces_exposes_no_capabilities(scoped):
    names = set(get_all_tools())
    assert not {n for n in names if "__" in n}


def test_scoped_namespace_exposes_only_its_capabilities(scoped):
    scoped(["press"])
    all_tools = get_all_tools()
    assert "press__draft" in all_tools and "press__publish" in all_tools
    assert "scan__status" not in all_tools
    assert all_tools["press__draft"].category == ActionCategory.WRITE
    assert all_tools["press__draft"].parameters["properties"]["source"]


def test_two_namespaces_union(scoped):
    scoped(["press", "scan"])
    assert {"press__draft", "press__publish", "scan__status"} <= set(get_all_tools())
    assert get_all_tools()["scan__status"].category == ActionCategory.READ


def test_setting_accepts_comma_separated_string(monkeypatch):
    class _Store:
        def get(self, key, default=None):
            return " press, scan ,"

    monkeypatch.setattr("axiom.extensions.builtins.settings.store.SettingsStore", lambda: _Store())
    assert tools._skill_namespaces() == ["press", "scan"]


def test_capability_never_shadows_an_existing_tool(monkeypatch, scoped):
    builtin = get_all_tools()["query_docs"]
    monkeypatch.setattr(
        tools,
        "_scan_skill_tools",
        lambda: {
            "query_docs": ToolDef(name="query_docs", description="x", category=ActionCategory.READ)
        },
    )
    assert get_all_tools()["query_docs"] is builtin


def test_tool_definitions_include_scoped_capabilities(scoped):
    scoped(["scan"])
    names = {d["function"]["name"] for d in tools.get_tool_definitions()}
    assert "scan__status" in names


def test_execute_routes_to_registry_invoke_with_mcp_shape(scoped, registry):
    scoped(["press"])
    result = execute_tool("press__draft", {"source": "notes.md"})
    assert result == {
        "ok": True,
        "value": {"rendered": "notes.md"},
        "errors": [],
        "actions_taken": ["drafted"],
    }
    assert registry.seen == [{"source": "notes.md"}]


def test_execute_of_a_failing_capability_is_a_result_not_an_exception(scoped):
    scoped(["press"])
    result = execute_tool("press__publish", {"source": "x"})
    assert result["ok"] is False
    assert any("upstream down" in e for e in result["errors"])


def test_unscoped_capability_is_not_executable(scoped):
    scoped(["press"])
    result = execute_tool("scan__status", {})
    assert "error" in result  # falls through to the unknown-tool path
