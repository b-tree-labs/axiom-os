# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for chat interaction modes (ask / plan / agent).

Each mode produces materially different turn behavior:

- ``agent`` (default) — tools available, full agentic loop. Current behavior.
- ``ask``  — no tools exposed; one model call, no tool-use rounds.
- ``plan`` — tools available BUT system prompt instructs the model to
  produce a plan only and stop without executing.

The Shift+Tab cycle in the fullscreen TUI must propagate the chosen mode
to the agent — previously this was a cosmetic toolbar relabel.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def stub_agent():
    from axiom.extensions.builtins.chat.agent import ChatAgent

    a = ChatAgent.__new__(ChatAgent)
    # Minimal init for unit-mode tests (we don't drive a real turn).
    a._interaction_mode = "agent"
    return a


def test_default_interaction_mode_is_agent(stub_agent):
    assert stub_agent._interaction_mode == "agent"


def test_set_interaction_mode_validates(stub_agent):
    from axiom.extensions.builtins.chat.agent import ChatAgent

    ChatAgent.set_interaction_mode(stub_agent, "ask")
    assert stub_agent._interaction_mode == "ask"

    ChatAgent.set_interaction_mode(stub_agent, "plan")
    assert stub_agent._interaction_mode == "plan"

    ChatAgent.set_interaction_mode(stub_agent, "agent")
    assert stub_agent._interaction_mode == "agent"


def test_set_interaction_mode_rejects_unknown(stub_agent):
    from axiom.extensions.builtins.chat.agent import ChatAgent

    with pytest.raises(ValueError, match="unknown interaction mode"):
        ChatAgent.set_interaction_mode(stub_agent, "wat")


def test_ask_mode_disables_tools_in_turn():
    """In ask mode, the gateway must be called with tools=None so the
    model cannot emit tool_use blocks. This is the security/UX contract."""
    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.infra.orchestrator.session import Session

    agent = ChatAgent(session=Session())
    agent.set_interaction_mode("ask")

    captured = {}

    def fake_complete(messages, system, tools=None, **kwargs):
        captured["tools"] = tools
        resp = MagicMock()
        resp.text = "an answer"
        resp.tool_use = []
        resp.input_tokens = 1
        resp.output_tokens = 1
        resp.cache_read_tokens = 0
        resp.model = "fake"
        return resp

    # Force the non-streaming path for testability.
    agent._render = None
    agent._renderer_callback = None

    with (
        patch.object(type(agent.gateway), "available", new=True),
        patch.object(agent.gateway, "complete_with_tools", side_effect=fake_complete),
        patch.object(agent, "_build_system_prompt", return_value=""),
    ):
        agent.turn("what is gravity?", stream=False)

    # The contract: ask mode hides the tool surface entirely.
    assert captured["tools"] is None or captured["tools"] == []


def test_plan_mode_strips_tools_from_api_call_but_lists_names_in_prompt():
    """In plan mode, tools=None is sent to the API (prevents tool calls),
    but tool names are injected into the system prompt so the model can
    reference them in its plan."""
    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.infra.orchestrator.session import Session

    agent = ChatAgent(session=Session())
    agent.set_interaction_mode("plan")

    captured = {}

    def fake_complete(messages, system, tools=None, **kwargs):
        captured["system"] = system
        captured["tools"] = tools
        resp = MagicMock()
        resp.text = "1. step one\n2. step two"
        resp.tool_use = []
        resp.input_tokens = 1
        resp.output_tokens = 1
        resp.cache_read_tokens = 0
        resp.model = "fake"
        return resp

    agent._render = None
    agent._renderer_callback = None

    with (
        patch.object(type(agent.gateway), "available", new=True),
        patch.object(agent.gateway, "complete_with_tools", side_effect=fake_complete),
        patch.object(agent, "_build_system_prompt", return_value="BASE PROMPT"),
    ):
        agent.turn("refactor the auth module", stream=False)

    # Tools stripped from API call
    assert captured["tools"] is None
    # Tool names still listed in system prompt
    assert "Available tools" in captured["system"]
    assert "write_file" in captured["system"]
    assert "PLAN MODE" in captured["system"]
    assert "BASE PROMPT" in captured["system"]
