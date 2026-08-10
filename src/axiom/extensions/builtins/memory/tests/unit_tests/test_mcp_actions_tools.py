# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Action-provenance query tools on the axiom-memory MCP server (#665).

``axiom_actions_recent`` + ``axiom_actions_search`` are thin calls into
``axiom.policy.action_ledger`` — the domain logic stays in the ledger
module, matching how the memory tools delegate to CompositionService.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def seeded_state_dir(tmp_path, monkeypatch):
    """A tmp state dir holding a JSONL ledger with three records, wired
    as the process state dir so the MCP tool functions resolve it."""
    from axiom.policy import action_ledger

    monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "jsonl")
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    action_ledger._reset_backend_cache()

    ledger = action_ledger.ActionLedger(state_dir=tmp_path)
    ledger.record_action(
        agent="tidy", op_class="git.branch.delete", name="prune_merged",
        candidate="feat/a", guards=["act"], outcome="proceeded",
        undo_ref="refs/tidy-archive/local/feat/a",
    )
    ledger.record_action(
        agent="tidy", op_class="git.branch.delete", name="prune_merged",
        candidate="feat/b", guards=["volume"], outcome="refused",
        refusing_rule="volume_limit_exceeded (12 > 10)",
    )
    ledger.record_action(
        agent="rivet", op_class="github.issue.close", name="auto_close",
        candidate="#42", guards=["act"], outcome="failed",
    )
    yield tmp_path
    action_ledger._reset_backend_cache()


def test_tool_list_includes_actions_tools():
    from axiom.extensions.builtins.memory import mcp_server
    tool_names = {t.name for t in mcp_server._TOOLS}
    assert "axiom_actions_recent" in tool_names
    assert "axiom_actions_search" in tool_names


def test_actions_tools_are_wired_into_handlers():
    from axiom.extensions.builtins.memory import mcp_server
    assert "axiom_actions_recent" in mcp_server._HANDLERS
    assert "axiom_actions_search" in mcp_server._HANDLERS


def test_recent_schema_has_n_and_agent():
    from axiom.extensions.builtins.memory import mcp_server
    tool = next(t for t in mcp_server._TOOLS if t.name == "axiom_actions_recent")
    props = tool.inputSchema["properties"]
    assert set(props) == {"n", "agent"}
    assert tool.inputSchema.get("required", []) == []


def test_search_schema_has_all_filters():
    from axiom.extensions.builtins.memory import mcp_server
    tool = next(t for t in mcp_server._TOOLS if t.name == "axiom_actions_search")
    props = tool.inputSchema["properties"]
    assert set(props) == {
        "agent", "op_class", "outcome", "since", "until", "text", "limit",
    }
    assert tool.inputSchema.get("required", []) == []


def test_actions_recent_returns_latest_first(seeded_state_dir):
    from axiom.extensions.builtins.memory import mcp_server
    out = mcp_server.actions_recent(n=2)
    assert out["count"] == 2
    assert out["actions"][0]["candidate"] == "#42"


def test_actions_recent_scopes_by_agent(seeded_state_dir):
    from axiom.extensions.builtins.memory import mcp_server
    out = mcp_server.actions_recent(n=10, agent="tidy")
    assert out["count"] == 2
    assert all(r["agent"] == "tidy" for r in out["actions"])


def test_actions_search_filters(seeded_state_dir):
    from axiom.extensions.builtins.memory import mcp_server
    out = mcp_server.actions_search(outcome="refused")
    assert out["count"] == 1
    assert out["actions"][0]["refusing_rule"].startswith("volume_limit_exceeded")

    out = mcp_server.actions_search(agent="tidy", op_class="git.branch.delete")
    assert out["count"] == 2

    out = mcp_server.actions_search(text="tidy-archive")
    assert out["count"] == 1
    assert out["actions"][0]["candidate"] == "feat/a"


def test_actions_search_since_window(seeded_state_dir):
    from axiom.extensions.builtins.memory import mcp_server
    assert mcp_server.actions_search(since="1h")["count"] == 3
    out = mcp_server.actions_search(since="garbage")
    assert out.get("error")
