# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``ChatAgent.turn(..., raw=True)`` — the benchmarking bypass.

Raw-model benchmark support: benchmarks need to measure raw model
quality vs. wrapped (RAG-augmented) quality on a 102-prompt verdict-task
corpus. ``raw=True`` must:

- skip the system prompt build (no identity/policies/RAG),
- skip RAG context retrieval entirely,
- skip the tool surface (single-shot, no tool loop),
- skip ``session.add_message`` (fully ephemeral — no session pollution),
- still emit the routing-classifier audit log (metadata, not augmentation).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from axiom.extensions.builtins.chat.agent import ChatAgent
from axiom.infra.bus import EventBus
from axiom.infra.gateway import CompletionResponse, Gateway
from axiom.infra.orchestrator.session import Session


@pytest.fixture
def mock_gateway():
    gw = MagicMock(spec=Gateway)
    gw.available = True
    gw.active_provider = MagicMock()
    gw.active_provider.name = "test"
    gw.active_provider.model = "qwen2.5-7b-instruct"
    return gw


@pytest.fixture
def agent(mock_gateway, tmp_path):
    bus = EventBus(log_path=tmp_path / "events.jsonl")
    session = Session()
    return ChatAgent(gateway=mock_gateway, bus=bus, session=session)


class TestRawSkipsAugmentation:
    """raw=True bypasses every augmentation layer."""

    def test_raw_skips_system_prompt(self, agent, mock_gateway):
        mock_gateway.complete_with_tools.return_value = CompletionResponse(
            text="hi", provider="test", success=True,
        )
        agent.turn("benchmark prompt", stream=False, raw=True)

        assert mock_gateway.complete_with_tools.call_count == 1
        kwargs = mock_gateway.complete_with_tools.call_args.kwargs
        assert kwargs["system"] == "", (
            f"raw mode must pass empty system prompt; got {kwargs['system']!r}"
        )

    def test_raw_skips_rag_context(self, agent, mock_gateway):
        """The gateway should receive only the user message, no RAG block."""
        mock_gateway.complete_with_tools.return_value = CompletionResponse(
            text="hi", provider="test", success=True,
        )
        agent.turn("benchmark prompt", stream=False, raw=True)

        kwargs = mock_gateway.complete_with_tools.call_args.kwargs
        messages = kwargs["messages"]
        assert messages == [{"role": "user", "content": "benchmark prompt"}]

    def test_raw_skips_tools(self, agent, mock_gateway):
        """Tools must be None — no tool surface, single-shot."""
        mock_gateway.complete_with_tools.return_value = CompletionResponse(
            text="hi", provider="test", success=True,
        )
        agent.turn("benchmark prompt", stream=False, raw=True)

        kwargs = mock_gateway.complete_with_tools.call_args.kwargs
        assert kwargs["tools"] is None
        # And only one call — no MAX_TOOL_ROUNDS loop in raw mode.
        assert mock_gateway.complete_with_tools.call_count == 1

    def test_raw_skips_session_add(self, agent, mock_gateway):
        """raw=True must not pollute the session with user/assistant messages."""
        mock_gateway.complete_with_tools.return_value = CompletionResponse(
            text="hi", provider="test", success=True,
        )
        before = len(agent.session.messages)
        agent.turn("benchmark prompt", stream=False, raw=True)
        after = len(agent.session.messages)
        assert before == after, (
            f"raw mode must be ephemeral; session grew from {before} to {after}"
        )

    def test_raw_still_emits_routing_audit(self, agent, mock_gateway):
        """Routing-classifier audit is metadata, not augmentation — keep it."""
        mock_gateway.complete_with_tools.return_value = CompletionResponse(
            text="hi", provider="test", success=True,
        )
        with patch(
            "axiom.infra.routing_audit.log_routing_decision"
        ) as mock_log:
            agent.turn("benchmark prompt", stream=False, raw=True)
            assert mock_log.call_count == 1, (
                "routing audit must still fire in raw mode"
            )

    def test_raw_returns_response_text(self, agent, mock_gateway):
        mock_gateway.complete_with_tools.return_value = CompletionResponse(
            text="raw model output here", provider="test", success=True,
        )
        out = agent.turn("benchmark prompt", stream=False, raw=True)
        assert out == "raw model output here"


class TestRawRegressions:
    """raw=False (default) must keep the full augmented pipeline."""

    def test_default_path_builds_system_prompt(self, agent, mock_gateway):
        mock_gateway.complete_with_tools.return_value = CompletionResponse(
            text="answer", provider="test", success=True,
        )
        with patch.object(
            agent, "_build_system_prompt", return_value="SYSTEM_PROMPT"
        ) as mock_build:
            agent.turn("normal prompt", stream=False)
            assert mock_build.call_count == 1

        kwargs = mock_gateway.complete_with_tools.call_args.kwargs
        assert kwargs["system"] == "SYSTEM_PROMPT"

    def test_default_path_passes_tools(self, agent, mock_gateway):
        mock_gateway.complete_with_tools.return_value = CompletionResponse(
            text="answer", provider="test", success=True,
        )
        agent.turn("normal prompt", stream=False)
        kwargs = mock_gateway.complete_with_tools.call_args.kwargs
        assert kwargs["tools"] is not None
        assert isinstance(kwargs["tools"], list)
        assert len(kwargs["tools"]) > 0

    def test_default_path_adds_session_messages(self, agent, mock_gateway):
        mock_gateway.complete_with_tools.return_value = CompletionResponse(
            text="answer", provider="test", success=True,
        )
        before = len(agent.session.messages)
        agent.turn("normal prompt", stream=False)
        after = len(agent.session.messages)
        # User + assistant = +2
        assert after - before == 2


class TestRawStreaming:
    """raw=True must work in streaming mode without injecting a system prompt."""

    def test_raw_streaming_does_not_crash(self, agent, mock_gateway):
        # Provide a stream producer for stream_with_tools so streaming path
        # can run end-to-end.
        from axiom.infra.gateway import StreamChunk

        def fake_stream(**kwargs):
            yield StreamChunk(type="text", text="hi")
            yield StreamChunk(
                type="usage", input_tokens=1, output_tokens=1, cache_read_tokens=0,
            )

        mock_gateway.stream_with_tools.side_effect = lambda **kw: fake_stream(**kw)
        mock_gateway.complete_with_tools.return_value = CompletionResponse(
            text="hi", provider="test", success=True,
        )

        # No render provider + no callback set — agent should still run, but
        # falls through the no-render branch of streaming. In raw mode we
        # actually short-circuit to non-streaming since the loop isn't useful;
        # either way it must not crash and must respect raw constraints.
        out = agent.turn("benchmark prompt", stream=True, raw=True)
        assert isinstance(out, str)

    def test_raw_streaming_no_system_prompt_injection(self, agent, mock_gateway):
        """Even when streaming, raw must not inject the system prompt."""
        mock_gateway.complete_with_tools.return_value = CompletionResponse(
            text="hi", provider="test", success=True,
        )
        # The agent's raw path takes the non-streaming branch (single-shot
        # is the contract), so we check complete_with_tools args.
        agent.turn("benchmark prompt", stream=True, raw=True)
        kwargs = mock_gateway.complete_with_tools.call_args.kwargs
        assert kwargs["system"] == ""
        assert kwargs["messages"] == [{"role": "user", "content": "benchmark prompt"}]
        assert kwargs["tools"] is None
