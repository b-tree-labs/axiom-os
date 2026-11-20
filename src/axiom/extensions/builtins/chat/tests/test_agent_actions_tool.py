# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Neut-chat tool for the agent action-provenance ledger (#665).

Registered through the existing tools_ext registry so Neut can answer
"what did TIDY do yesterday" — a thin read-only delegate to
``axiom.policy.action_ledger``.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.chat.tools_ext import agent_actions
from axiom.infra.orchestrator.actions import ActionCategory


@pytest.fixture
def seeded_state_dir(tmp_path, monkeypatch):
    from axiom.policy import action_ledger
    monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "jsonl")
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    action_ledger._reset_backend_cache()
    ledger = action_ledger.ActionLedger(state_dir=tmp_path)
    ledger.record_action(
        agent="tidy", op_class="git.branch.delete", name="prune_merged",
        candidate="feat/a", guards=["act"], outcome="proceeded",
    )
    ledger.record_action(
        agent="rivet", op_class="github.issue.close", name="auto_close",
        candidate="#42", guards=["hard_disable"], outcome="refused",
        refusing_rule="hard_disable",
    )
    yield tmp_path
    action_ledger._reset_backend_cache()


def test_tool_is_discoverable_and_read_only():
    (tool,) = agent_actions.TOOLS
    assert tool.name == "agent_actions"
    assert tool.category is ActionCategory.READ
    props = tool.parameters["properties"]
    assert {"agent", "op_class", "outcome", "since", "until", "text", "limit"} <= set(props)


def test_execute_queries_the_ledger(seeded_state_dir):
    out = agent_actions.execute("agent_actions", {"agent": "tidy"})
    assert out["count"] == 1
    assert out["actions"][0]["candidate"] == "feat/a"


def test_execute_surfaces_refusals(seeded_state_dir):
    out = agent_actions.execute("agent_actions", {"outcome": "refused"})
    assert out["count"] == 1
    assert out["actions"][0]["refusing_rule"] == "hard_disable"


def test_registry_scan_picks_up_the_tool():
    from axiom.extensions.builtins.chat.tools import get_all_tools
    assert "agent_actions" in get_all_tools()
