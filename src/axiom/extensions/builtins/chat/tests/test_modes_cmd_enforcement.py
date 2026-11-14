# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Plan and ask mode refuse tool calls at the dispatch layer, not only by withholding the tool list.

A provider can ignore ``tools=None`` and a model can write a call as text (tool_call_recovery turns that into
a real call). The request-side strip does not stop either; the dispatch check does.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.chat.agent import ChatAgent
from axiom.extensions.builtins.chat.tools import ToolDef
from axiom.infra.orchestrator.actions import ActionCategory
from axiom.infra.orchestrator.session import Session
from axiom.llm.gateway import CompletionResponse, ToolUseBlock


@pytest.fixture
def world(tmp_path):
    ran: list[str] = []

    def writer(**kw):
        (tmp_path / "touched").write_text("x")
        ran.append("writer")
        return {"ok": True}

    def reader(**kw):
        ran.append("reader")
        return {"ok": True}

    tools = {
        "t_write": ToolDef("t_write", "w", ActionCategory.WRITE, {}, writer),
        "t_read": ToolDef("t_read", "r", ActionCategory.READ, {}, reader),
    }
    agent = ChatAgent(session=Session())
    agent._approval_policy = None
    return agent, tools, ran, tmp_path


def _calls(*names):
    return CompletionResponse(
        text="", tool_use=[ToolUseBlock(tool_id=f"id{i}", name=n, input={}) for i, n in enumerate(names)]
    )


def _process(agent, tools, *names):
    return agent._process_tool_calls(_calls(*names), tools)


def test_plan_mode_refuses_a_mutating_call_and_never_runs_it(world):
    agent, tools, ran, tmp = world
    agent.set_interaction_mode("plan")
    agent._begin_turn_mode()
    results = _process(agent, tools, "t_write")
    assert ran == []
    assert not (tmp / "touched").exists()
    assert "plan mode" in results[0][2]["error"].lower()


def test_plan_mode_really_does_not_write_with_the_real_write_file_tool(tmp_path):
    from axiom.extensions.builtins.chat.tools import get_all_tools

    target = tmp_path / "out.txt"
    agent = ChatAgent(session=Session())
    agent.set_interaction_mode("plan")
    agent._begin_turn_mode()
    resp = CompletionResponse(
        tool_use=[ToolUseBlock("i", "write_file", {"file_path": str(target), "content": "x"})]
    )
    results = agent._process_tool_calls(resp, get_all_tools())
    assert not target.exists()
    assert "plan mode" in results[0][2]["error"].lower()


def test_ask_mode_refuses_every_tool_call(world):
    agent, tools, ran, _ = world
    agent.set_interaction_mode("ask")
    agent._begin_turn_mode()
    results = _process(agent, tools, "t_read", "t_write")
    assert ran == []
    assert all("ask mode" in r[2]["error"].lower() for r in results)


def test_plan_mode_still_lets_a_read_classified_call_run(world):
    agent, tools, ran, _ = world
    agent.set_interaction_mode("plan")
    agent._begin_turn_mode()
    results = _process(agent, tools, "t_read")
    assert "mode" not in str(results[0][2].get("error", ""))


def test_agent_mode_is_unchanged_for_reads(world):
    agent, tools, ran, _ = world
    agent.set_interaction_mode("agent")
    agent._begin_turn_mode()
    results = _process(agent, tools, "t_read")
    assert "mode" not in str(results[0][2].get("error", ""))


def test_one_shot_mode_applies_to_exactly_one_turn(world):
    agent, _, _, _ = world
    agent.set_interaction_mode("agent")
    agent.request_next_turn_mode("plan")
    assert agent._begin_turn_mode() == "plan"
    assert agent._interaction_mode == "agent"  # the standing mode never changed
    assert agent._begin_turn_mode() == "agent"  # consumed


def test_one_shot_rejects_unknown_mode(world):
    agent, *_ = world
    with pytest.raises(ValueError):
        agent.request_next_turn_mode("wat")


def test_a_one_shot_plan_turn_runs_without_tools_then_the_standing_mode_returns():
    """End to end through turn(): the request carries no tools for the one-shot turn, and the next turn
    is back in the standing (agent) mode with its tools."""
    from unittest.mock import patch

    agent = ChatAgent(session=Session())
    agent.set_interaction_mode("agent")
    agent._render = None
    agent._renderer_callback = None
    seen = []

    def fake_complete(messages, system, tools=None, **kw):
        seen.append((tools, system))
        return CompletionResponse(text="done", model="fake")

    with (
        patch.object(type(agent.gateway), "available", new=True),
        patch.object(agent.gateway, "complete_with_tools", side_effect=fake_complete),
        patch.object(agent, "_build_system_prompt", return_value="BASE"),
    ):
        agent.request_next_turn_mode("plan")
        agent.turn("review this", stream=False)
        agent.turn("and now act", stream=False)
    assert seen[0][0] is None and "PLAN MODE" in seen[0][1]
    assert seen[1][0] and "PLAN MODE" not in seen[1][1]
