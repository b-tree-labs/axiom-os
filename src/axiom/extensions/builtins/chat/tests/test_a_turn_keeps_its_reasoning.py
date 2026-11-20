# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A turn keeps the model's reasoning on its scope, beside the answer, for a surface that shows it.

``turn`` returns the answer text only, and the reasoning of every round was dropped (a streamed round rendered it to
a terminal and then discarded it). A serving surface could not hand it to a client that folds it away, and could not
tell when the "answer" it returned was the model's reasoning because there was no answer.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.chat.agent import ChatAgent
from axiom.extensions.builtins.chat.scope import ChatScope
from axiom.infra.bus import EventBus
from axiom.infra.gateway import CompletionResponse, Gateway, ToolUseBlock
from axiom.infra.orchestrator.session import Session
from axiom.llm.gateway import StreamChunk


@pytest.fixture
def mock_gateway():
    gw = MagicMock(spec=Gateway)
    gw.available = True
    gw.active_provider = MagicMock()
    gw.active_provider.name = "test"
    gw.active_provider.model = "test-model"
    return gw


@pytest.fixture
def agent(mock_gateway, tmp_path):
    return ChatAgent(gateway=mock_gateway, bus=EventBus(log_path=tmp_path / "events.jsonl"), session=Session())


def _answer(text="0.127 m", reasoning="", reasoning_only=False):
    return CompletionResponse(text=text, reasoning=reasoning, reasoning_only=reasoning_only, provider="test",
                              success=True)


def test_a_non_streamed_turn_keeps_its_reasoning(agent, mock_gateway):
    scope = ChatScope(session=Session())
    mock_gateway.complete_with_tools.return_value = _answer(reasoning="Check the table first.")
    assert "0.127 m" in agent.turn("What is the radius?", stream=False, scope=scope)
    assert scope.last_reasoning == "Check the table first."
    assert scope.last_reasoning_only is False


def test_reasoning_from_every_round_is_kept_in_order(agent, mock_gateway):
    scope = ChatScope(session=Session(), max_tool_rounds=5)
    calls = {"n": 0}

    def respond(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return CompletionResponse(text="", reasoning="I should look it up.", provider="test", success=True,
                                      tool_use=[ToolUseBlock(tool_id="t1", name="list_providers", input={})],
                                      stop_reason="tool_use")
        return _answer(reasoning="The result answers it.")

    mock_gateway.complete_with_tools.side_effect = respond
    agent.turn("What is the radius?", stream=False, scope=scope)
    assert scope.last_reasoning == "I should look it up.\n\nThe result answers it."


def test_a_turn_with_no_answer_says_so(agent, mock_gateway):
    scope = ChatScope(session=Session())
    mock_gateway.complete_with_tools.return_value = _answer(text="Still thinking about", reasoning="Still thinking about",
                                                            reasoning_only=True)
    agent.turn("What is the radius?", stream=False, scope=scope)
    assert scope.last_reasoning_only is True


def test_a_streamed_turn_keeps_its_reasoning(agent, mock_gateway):
    scope = ChatScope(session=Session())
    scope.on_chunk = lambda chunk: None  # a serving surface watching the stream
    mock_gateway.stream_with_tools.side_effect = lambda *a, **k: iter([
        StreamChunk(type="thinking_start"),
        StreamChunk(type="thinking_delta", text="Check the "),
        StreamChunk(type="thinking_delta", text="table first."),
        StreamChunk(type="thinking_end"),
        StreamChunk(type="text", text="0.127 m"),
    ])
    agent._render = None
    assert "0.127 m" in agent.turn("What is the radius?", stream=True, scope=scope)
    assert scope.last_reasoning == "Check the table first."
    assert scope.last_reasoning_only is False


def test_the_next_turn_starts_without_the_last_ones_reasoning(agent, mock_gateway):
    scope = ChatScope(session=Session())
    mock_gateway.complete_with_tools.return_value = _answer(reasoning="Old thinking.", reasoning_only=True)
    agent.turn("first", stream=False, scope=scope)
    mock_gateway.complete_with_tools.return_value = _answer()
    agent.turn("second", stream=False, scope=scope)
    assert (scope.last_reasoning, scope.last_reasoning_only) == ("", False)
