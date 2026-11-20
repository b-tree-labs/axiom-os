# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""/plan, /ask, /agent and /mode switch the agent's interaction mode and state what is really enforced."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.chat.agent import ChatAgent
from axiom.extensions.builtins.chat.builtin_commands import ensure_loaded
from axiom.extensions.builtins.chat.command_registry import REGISTRY, CommandContext
from axiom.infra.orchestrator.session import Session


@pytest.fixture
def agent():
    return ChatAgent(session=Session())


def _run(agent, line):
    ensure_loaded()
    return REGISTRY.dispatch(line, CommandContext(agent=agent, store=None))


@pytest.mark.parametrize("mode", ["ask", "plan", "agent"])
def test_command_sets_the_agents_mode(agent, mode):
    other = "agent" if mode != "agent" else "ask"
    agent.set_interaction_mode(other)
    out = _run(agent, f"/{mode}")
    assert agent._interaction_mode == mode
    assert mode in out.lower()


def test_mode_reports_current_without_changing_it(agent):
    agent.set_interaction_mode("plan")
    out = _run(agent, "/mode")
    assert agent._interaction_mode == "plan"
    assert "plan" in out.lower()


def test_ask_states_that_no_tools_are_offered(agent):
    out = _run(agent, "/ask")
    assert "no tools are offered" in out.lower()


def test_plan_states_it_is_not_a_sandbox_and_what_is_enforced(agent):
    out = _run(agent, "/plan").lower()
    assert "not a sandbox" in out
    assert "stripped" in out
    assert "refused at dispatch" in out


def test_agent_mode_states_full_tool_loop(agent):
    out = _run(agent, "/agent").lower()
    assert "tools" in out and "approval" in out


def test_modes_are_listed_in_help_and_completion():
    ensure_loaded()
    meta = REGISTRY.meta()
    for name in ("/plan", "/ask", "/agent", "/mode"):
        assert name in meta
    assert "/plan" in REGISTRY.help_text()


def test_status_carries_the_mode_line(agent):
    from axiom.extensions.builtins.chat.commands import cmd_status

    agent.set_interaction_mode("plan")
    assert "plan" in cmd_status(agent).lower().split("mode:")[1][:20]
