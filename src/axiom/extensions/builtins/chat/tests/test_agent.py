# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the chat agent — native tool-use loop."""

from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.chat.agent import ChatAgent
from axiom.infra.bus import EventBus
from axiom.infra.gateway import (
    CompletionResponse,
    Gateway,
    GatewayResponse,
    ToolUseBlock,
)
from axiom.infra.orchestrator.session import Session


class TestChatAgent:
    """Test the chat agent native tool-use loop."""

    @pytest.fixture
    def mock_gateway(self):
        gw = MagicMock(spec=Gateway)
        gw.available = True
        gw.active_provider = MagicMock()
        gw.active_provider.name = "test"
        gw.active_provider.model = "test-model"
        return gw

    @pytest.fixture
    def agent(self, mock_gateway, tmp_path):
        bus = EventBus(log_path=tmp_path / "events.jsonl")
        session = Session()
        return ChatAgent(gateway=mock_gateway, bus=bus, session=session)

    def test_simple_turn(self, agent, mock_gateway):
        """Agent processes a simple turn without tool calls."""
        mock_gateway.complete_with_tools.return_value = CompletionResponse(
            text="I can help with that!",
            provider="test",
            success=True,
        )

        response = agent.turn("What documents are tracked?", stream=False)

        assert "I can help" in response
        assert len(agent.session.messages) == 2  # user + assistant
        assert agent.session.messages[0].role == "user"
        assert agent.session.messages[1].role == "assistant"

    def test_turn_with_tool_call(self, agent, mock_gateway):
        """Agent executes tool calls from structured response."""
        # First call returns tool use
        mock_gateway.complete_with_tools.side_effect = [
            CompletionResponse(
                text="Let me check.",
                tool_use=[ToolUseBlock(tool_id="t1", name="list_providers", input={})],
                provider="test",
                success=True,
                stop_reason="tool_use",
            ),
            # Second call (after tool result) returns text
            CompletionResponse(
                text="Here are the providers.",
                provider="test",
                success=True,
            ),
        ]

        _response = agent.turn("What providers are available?", stream=False)

        # Should have called complete_with_tools twice (tool loop)
        assert mock_gateway.complete_with_tools.call_count >= 1

    def test_final_round_strips_tools_and_synthesizes(self, agent, mock_gateway):
        """A tool-spamming model still yields a real answer, never the apology.

        On the last allowed round the agent must offer NO tools, forcing the
        model to synthesize a text answer from everything already gathered —
        instead of running out of rounds and returning the
        "reached the maximum number of tool-use rounds" fallback as if it
        were a successful completion.
        """
        from axiom.extensions.builtins.chat.agent import MAX_TOOL_ROUNDS

        def respond(*args, **kwargs):
            # A well-behaved model: answers when given no tools, otherwise
            # keeps asking for more tool calls forever.
            if not kwargs.get("tools"):
                return CompletionResponse(
                    text="Synthesized answer from gathered context.",
                    provider="test",
                    success=True,
                )
            return CompletionResponse(
                text="",
                tool_use=[ToolUseBlock(tool_id="t1", name="list_providers", input={})],
                provider="test",
                success=True,
                stop_reason="tool_use",
            )

        mock_gateway.complete_with_tools.side_effect = respond

        response = agent.turn("Keep searching forever.", stream=False)

        assert "maximum number of tool-use rounds" not in response
        assert "Synthesized answer" in response
        # Loop terminates within the cap; the final call offered no tools.
        assert mock_gateway.complete_with_tools.call_count <= MAX_TOOL_ROUNDS
        assert mock_gateway.complete_with_tools.call_args.kwargs.get("tools") in (None, [])

    def test_session_accumulates_messages(self, agent, mock_gateway):
        """Messages accumulate across turns."""
        mock_gateway.complete_with_tools.return_value = CompletionResponse(
            text="First response",
            provider="test",
            success=True,
        )
        agent.turn("First message", stream=False)

        mock_gateway.complete_with_tools.return_value = CompletionResponse(
            text="Second response",
            provider="test",
            success=True,
        )
        agent.turn("Second message", stream=False)

        assert len(agent.session.messages) == 4  # 2 user + 2 assistant

    def test_legacy_fallback_no_providers(self, agent, mock_gateway):
        """Falls back to legacy mode when gateway unavailable."""
        mock_gateway.available = False
        mock_gateway.complete.return_value = GatewayResponse(
            text="Stub response",
            provider="stub",
            success=False,
        )

        response = agent.turn("test", stream=False)
        assert "Stub response" in response

    def test_legacy_tool_call_parsing(self, agent):
        """Legacy [tool: name] format is parsed correctly."""
        calls = agent._parse_legacy_tool_calls(
            'Here are the results:\n[tool: query_docs] {"file": "test.md"}\nDone.'
        )
        assert len(calls) == 1
        assert calls[0].name == "query_docs"
        assert calls[0].input["file"] == "test.md"

    def test_legacy_no_tool_calls(self, agent):
        """No tool calls in plain text."""
        calls = agent._parse_legacy_tool_calls("This is just a regular response.")
        assert calls == []

    def test_legacy_empty_params(self, agent):
        """Legacy tool call with no params."""
        calls = agent._parse_legacy_tool_calls("[tool: signal_status]")
        assert len(calls) == 1
        assert calls[0].input == {}


class TestSystemPrompt:
    """Test dynamic system prompt construction."""

    def test_base_prompt_always_present(self, tmp_path):
        session = Session()
        agent = ChatAgent(session=session)
        prompt = agent._build_system_prompt()
        # Prompt should contain the product identity (branding-dependent)
        assert "assistant" in prompt.lower()

    def test_context_file_included(self, tmp_path):
        session = Session(context={"file_content": "Custom context here"})
        agent = ChatAgent(session=session)
        prompt = agent._build_system_prompt()
        assert "Custom context here" in prompt


class TestContextWindowManagement:
    """Test message trimming for context budget."""

    def test_short_history_unchanged(self, tmp_path):
        session = Session()
        agent = ChatAgent(session=session)
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ]
        result = agent._trim_messages(messages)
        assert len(result) == 2

    def test_long_history_trimmed(self, tmp_path):
        session = Session()
        agent = ChatAgent(session=session)
        # Create messages that exceed budget
        messages = []
        for _ in range(100):
            messages.append({"role": "user", "content": "x" * 5000})
            messages.append({"role": "assistant", "content": "y" * 5000})

        result = agent._trim_messages(messages, system="system")
        assert len(result) < len(messages)
        # Should keep recent messages
        assert result[-1] == messages[-1]

    def test_drops_oldest_and_injects_summary(self, tmp_path):
        """T0-2 replaces the old 'keep first message' rule with a
        summary-of-dropped-history system message prepended when any
        trimming occurred. Preserves model awareness of earlier turns
        without holding the full text."""
        session = Session()
        agent = ChatAgent(session=session)
        messages = [
            {"role": "user", "content": "Initial question"},
        ]
        for _ in range(50):
            messages.append({"role": "assistant", "content": "a" * 2000})
            messages.append({"role": "user", "content": "b" * 2000})

        result = agent._trim_messages(messages, system="sys")
        assert len(result) < len(messages)
        assert result[0]["role"] == "system"
        assert "omitted" in result[0]["content"].lower()


class TestBuildMessages:
    """Test message building from session history."""

    def test_empty_session(self):
        session = Session()
        agent = ChatAgent(session=session)
        messages = agent._build_messages()
        assert messages == []

    def test_messages_from_session(self):
        session = Session()
        session.add_message("user", "Hello")
        session.add_message("assistant", "Hi!")
        agent = ChatAgent(session=session)
        messages = agent._build_messages()
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Hello"
        assert messages[1]["role"] == "assistant"
        assert messages[1]["content"] == "Hi!"


class TestCancelEvent:
    """T2.1 — threading.Event cancellation for in-flight turns."""

    @pytest.fixture
    def mock_gateway(self):
        gw = MagicMock(spec=Gateway)
        gw.available = True
        gw.active_provider = MagicMock()
        gw.active_provider.name = "test"
        gw.active_provider.model = "test-model"
        return gw

    @pytest.fixture
    def agent(self, mock_gateway, tmp_path):
        bus = EventBus(log_path=tmp_path / "events.jsonl")
        return ChatAgent(gateway=mock_gateway, bus=bus)

    def test_cancel_event_short_circuits_streaming_turn(self, agent, mock_gateway):
        """Pre-set cancel event causes streaming turn to raise ChatTurnCancelled."""
        from axiom.extensions.builtins.chat.agent import ChatTurnCancelled
        from axiom.infra.gateway import StreamChunk

        chunks_consumed = []

        def counting_renderer(chunks_iter):
            for c in chunks_iter:
                chunks_consumed.append(c)
            return ""

        agent.set_renderer(counting_renderer)

        mock_gateway.stream_with_tools.return_value = iter(
            [StreamChunk(type="text", text=f"chunk{i}") for i in range(5)]
            + [StreamChunk(type="done")]
        )

        agent._cancel_event.set()

        with pytest.raises(ChatTurnCancelled):
            agent._streaming_turn([], "", [], "any")

        # At most 1 chunk was yielded before cancellation fired
        assert len(chunks_consumed) <= 1

    def test_cancel_event_short_circuits_tool_loop(self, agent):
        """Cancel set after first dispatch skips remaining tool calls."""
        from unittest.mock import patch

        from axiom.infra.gateway import CompletionResponse, ToolUseBlock

        dispatched = []

        def fake_dispatch(tool_name, args, principal, eventbus, dispatcher, ext_origin):
            dispatched.append(tool_name)
            agent.cancel()  # trigger cancel after first dispatch
            return {"ok": True}

        tool_use = [
            ToolUseBlock(tool_id="t1", name="query_docs", input={}),
            ToolUseBlock(tool_id="t2", name="query_docs", input={}),
            ToolUseBlock(tool_id="t3", name="query_docs", input={}),
        ]
        response = CompletionResponse(
            text="", tool_use=tool_use, provider="test", model="test", success=True
        )

        with patch("axiom.infra.tool_gateway.dispatch_tool", side_effect=fake_dispatch):
            results = agent._process_tool_calls(response)

        assert len(dispatched) == 1, "only first tool should have dispatched"
        cancelled = [r for _, _, r in results if r.get("cancelled")]
        assert len(cancelled) == 2, "remaining two should have cancellation results"

    def test_reset_cancel_clears_event(self, agent):
        """reset_cancel() lets subsequent turns proceed normally."""
        agent.cancel()
        assert agent.is_cancelled()
        agent.reset_cancel()
        assert not agent.is_cancelled()


class TestPerPromptOverrideIntegration:
    """spec-chat-model-picker §3: `@provider` and `/m provider` overrides
    flip the gateway provider for one turn only."""

    @pytest.fixture
    def mock_gateway(self):
        gw = MagicMock(spec=Gateway)
        gw.available = True
        gw.active_provider = MagicMock()
        gw.active_provider.name = "test"
        gw.active_provider.model = "test-model"
        # Real provider list backs override resolution
        prov_anth = MagicMock()
        prov_anth.name = "anthropic"
        prov_open = MagicMock()
        prov_open.name = "openai"
        gw.providers = [prov_anth, prov_open]
        gw._provider_override = None
        return gw

    @pytest.fixture
    def agent(self, mock_gateway, tmp_path):
        bus = EventBus(log_path=tmp_path / "events.jsonl")
        session = Session()
        return ChatAgent(gateway=mock_gateway, bus=bus, session=session)

    def test_at_prefix_flips_override_and_strips_prompt(self, agent, mock_gateway):
        mock_gateway.complete_with_tools.return_value = CompletionResponse(
            text="ok", provider="anthropic", success=True
        )

        agent.turn("@anthropic explain X", stream=False)

        # set_provider_override called with anthropic, then restored to None
        assert mock_gateway.set_provider_override.call_args_list[0].args == ("anthropic",)
        assert mock_gateway.set_provider_override.call_args_list[-1].args == (None,)

        # gateway received the stripped prompt, NOT the original with @anthropic
        call_messages = mock_gateway.complete_with_tools.call_args.kwargs.get(
            "messages"
        ) or mock_gateway.complete_with_tools.call_args.args[0]
        last_user_content = call_messages[-1].get("content", "")
        if isinstance(last_user_content, list):
            # Multimodal — concatenate text parts
            last_user_content = "".join(
                p.get("text", "") for p in last_user_content if isinstance(p, dict)
            )
        assert "@anthropic" not in last_user_content
        assert "explain X" in last_user_content

    def test_unknown_provider_returns_inline_error_without_calling_gateway(
        self, agent, mock_gateway
    ):
        response = agent.turn("@nonsense explain X", stream=False)

        assert "Unknown provider 'nonsense'" in response
        mock_gateway.complete_with_tools.assert_not_called()
        # Gateway override left untouched
        mock_gateway.set_provider_override.assert_not_called()

    def test_plain_prompt_does_not_touch_override(self, agent, mock_gateway):
        mock_gateway.complete_with_tools.return_value = CompletionResponse(
            text="ok", provider="test", success=True
        )

        agent.turn("explain X", stream=False)

        mock_gateway.set_provider_override.assert_not_called()
