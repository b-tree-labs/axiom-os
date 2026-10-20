# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``/skills <name>``: schema, permission, read/write, and the CLI verb and MCP tool the registry maps to."""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.chat import tools
from axiom.extensions.builtins.chat.commands_ext import skills_cmd
from axiom.infra.skills import SkillRegistry, SkillResult, SkillSpec

from ._sa_helpers import make_agent, run


def _ok(p, c):
    return SkillResult(ok=True)


@pytest.fixture(autouse=True)
def _world(monkeypatch):
    from axiom.extensions import discovery

    monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "jsonl")
    monkeypatch.setattr(tools, "_manifest_roots", lambda: [])
    monkeypatch.setattr(discovery, "discover_and_load_chat_tools", lambda *a, **k: [])
    r = SkillRegistry()
    r.register_skill(SkillSpec(name="press.draft", fn=_ok, description="Draft a document.",
                               inputs={"source": "Path!", "style": "str"}, side_effects=True,
                               surfaces=("cli", "mcp", "agent_tool")))
    r.register_skill(SkillSpec(name="scan.status", fn=_ok, description="Scan status.", side_effects=False,
                               surfaces=("agent_tool",)))
    monkeypatch.setattr(tools, "_skill_registry", lambda: r)
    monkeypatch.setattr(skills_cmd, "discover_catalog", lambda **k: SkillRegistry())
    monkeypatch.setattr(tools, "_skill_namespaces", lambda: ["press"])


def test_detail_names_parameters_with_required_and_optional():
    out = run("/skills press.draft")
    assert "source  (Path, required)" in out and "style  (str, optional)" in out


def test_detail_says_mutating_or_read_only_from_the_declaration():
    assert "WRITE (held for approval)" in run("/skills press.draft")
    assert "read (auto-approved)" in run("/skills scan.status")


def test_detail_cli_verb_and_mcp_tool_come_from_the_declared_surfaces():
    out = run("/skills press.draft")
    assert "axi press draft" in out
    from axiom.extensions.builtins.mcp.skill_tools import mcp_tool_name

    assert mcp_tool_name("press.draft") in out  # the registry's own mapping, not a guess here
    other = run("/skills scan.status")
    assert "not declared on the cli surface" in other and "MCP tool    not exposed" in other


def test_detail_permission_is_the_one_the_approval_gate_reads():
    agent = make_agent()
    assert "permission  ask (default)" in run("/skills press.draft", agent)
    agent.permissions.set("press__draft", "deny")
    out = run("/skills press.draft", agent)
    assert "permission  deny" in out and "(default)" not in out.split("permission")[1].splitlines()[0]


def test_detail_for_a_skill_that_cannot_be_called_gives_the_fix():
    out = run("/skills scan.status")
    assert "no: namespace not enabled" in out
    assert "axi settings set chat.tool_namespaces press,scan" in out


def test_detail_json():
    data = json.loads(run("/skills press.draft --json"))
    row = data["rows"][0]
    assert row["name"] == "press.draft" and row["tool_name"] == "press__draft"
    assert row["mcp_exposed"] is True and row["inputs"] == {"source": "Path!", "style": "str"}


def test_pure_function_returns_dataclasses_without_a_chat_session():
    row = skills_cmd.describe_skill("press.draft")
    assert row is not None and row.callable_now and row.cli_verb == "axi press draft"
    assert skills_cmd.describe_skill("nope.nope") is None
