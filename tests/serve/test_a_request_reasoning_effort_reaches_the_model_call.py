# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""An OpenAI request's ``reasoning_effort`` reaches the agent's model calls on the serving path.

Open WebUI sends its "Reasoning Effort" setting as ``reasoning_effort``. The transport forwarded only ``max_tokens``,
``temperature``, ``tools`` and ``tool_choice``, so the setting was dropped before the agent ran. It now travels on
the request's own scope (never on shared state, so two callers' efforts cannot mix), and a value no model takes is
refused with a 400 before anything runs.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.chat.headless import HeadlessChat
from axiom.infra.bus import EventBus
from axiom.infra.gateway import CompletionResponse, Gateway, StreamChunk
from axiom.serve import ChatCompletionError, ChatCompletionsHandler
from axiom.serve.agent_backend import AgentChatBackend


@pytest.fixture
def mock_gateway():
    g = MagicMock(spec=Gateway)
    g.available = True
    g.active_provider = MagicMock()
    g.active_provider.name = "test"
    g.active_provider.model = "test-model"
    g.providers = [g.active_provider]
    g.complete_with_tools.return_value = CompletionResponse(text="ok", provider="test", success=True)
    g.stream_with_tools.side_effect = lambda **_k: iter([StreamChunk(type="text", text="ok")])
    return g


@pytest.fixture
def backend(mock_gateway, tmp_path):
    chat = HeadlessChat(turn_deadline=30.0, gateway=mock_gateway, bus=EventBus(log_path=tmp_path / "events.jsonl"))
    return AgentChatBackend(chat)


def _user(text="What is the core radius?"):
    return [{"role": "user", "content": text}]


def test_a_non_streamed_request_passes_it_to_the_model_call(backend, mock_gateway):
    backend(_user(), model="m", trace_id="t", reasoning_effort="low")
    assert mock_gateway.complete_with_tools.call_args.kwargs["reasoning_effort"] == "low"


def test_a_streamed_request_passes_it_to_the_model_call(backend, mock_gateway):
    assert "".join(p for p in backend.stream(_user(), model="m", trace_id="t", reasoning_effort="none")
                   if isinstance(p, str)) == "ok"
    assert mock_gateway.stream_with_tools.call_args.kwargs["reasoning_effort"] == "none"


def test_without_it_the_provider_default_applies(backend, mock_gateway):
    backend(_user(), model="m", trace_id="t")
    assert mock_gateway.complete_with_tools.call_args.kwargs["reasoning_effort"] is None


def test_an_effort_no_model_takes_is_refused(backend, mock_gateway):
    with pytest.raises(ChatCompletionError) as err:
        backend(_user(), model="m", trace_id="t", reasoning_effort="maximum")
    assert err.value.status_code == 400 and err.value.param == "reasoning_effort"
    mock_gateway.complete_with_tools.assert_not_called()


def test_the_transport_forwards_it_from_the_request(backend, mock_gateway):
    handler = ChatCompletionsHandler(backend=backend)
    handler.handle({"model": "m", "messages": _user(), "reasoning_effort": "high"})
    assert mock_gateway.complete_with_tools.call_args.kwargs["reasoning_effort"] == "high"
