# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Picker `@provider` override survives a multi-round tool-use loop.

Spec-chat-model-picker §3 says the override applies to "that turn"
including subsequent gateway calls inside the tool-use loop. Unit tests
in test_agent.py confirm it fires on a single round; this test loops
twice and asserts:

1. The override is in effect across every gateway call within the turn.
2. The override is restored exactly once, after the loop completes.
3. A failure mid-loop still restores the override.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.chat.agent import ChatAgent
from axiom.infra.bus import EventBus
from axiom.infra.gateway import CompletionResponse, Gateway, ToolUseBlock
from axiom.infra.orchestrator.session import Session


@pytest.fixture
def mock_gateway():
    gw = MagicMock(spec=Gateway)
    gw.available = True
    gw.active_provider = MagicMock()
    gw.active_provider.name = "test"
    gw.active_provider.model = "test-model"
    prov_anth = MagicMock()
    prov_anth.name = "anthropic"
    prov_open = MagicMock()
    prov_open.name = "openai"
    gw.providers = [prov_anth, prov_open]
    gw._provider_override = "openai"  # session default before the override
    return gw


@pytest.fixture
def agent(mock_gateway, tmp_path):
    bus = EventBus(log_path=tmp_path / "events.jsonl")
    session = Session()
    return ChatAgent(gateway=mock_gateway, bus=bus, session=session)


class TestPickerSurvivesToolUseLoop:
    def test_override_persists_across_rounds_then_restores(self, agent, mock_gateway):
        """A turn with one tool-use round + final text both run under the
        per-prompt override; the prior session override is restored when
        the loop finishes."""
        # Round 1: tool call. Round 2: final text.
        mock_gateway.complete_with_tools.side_effect = [
            CompletionResponse(
                text="Let me look that up.",
                tool_use=[ToolUseBlock(tool_id="t1", name="list_providers", input={})],
                provider="anthropic",
                success=True,
                stop_reason="tool_use",
            ),
            CompletionResponse(
                text="Here's the answer.",
                provider="anthropic",
                success=True,
            ),
        ]

        agent.turn("@anthropic what providers do you know?", stream=False)

        # Override sequence: set anthropic on entry, restored to openai on exit.
        # Should be exactly two calls: one to switch in, one to switch out.
        # (NOT one set per round — the with-block holds it for the whole turn.)
        names = [c.args[0] for c in mock_gateway.set_provider_override.call_args_list]
        assert names == ["anthropic", "openai"], (
            f"override should be flipped once + restored once, got {names}"
        )
        # Gateway received >=1 call (tool-use round + final round)
        assert mock_gateway.complete_with_tools.call_count >= 2

    def test_override_restored_even_when_tool_dispatch_raises(
        self, agent, mock_gateway
    ):
        """If the tool-loop blows up mid-flight, the with-block's __exit__
        still restores the prior override."""
        mock_gateway.complete_with_tools.side_effect = RuntimeError(
            "simulated provider hiccup"
        )

        with pytest.raises(RuntimeError):
            agent.turn("@anthropic crash this", stream=False)

        names = [c.args[0] for c in mock_gateway.set_provider_override.call_args_list]
        assert names == ["anthropic", "openai"], (
            "override must still be restored after an exception inside the turn"
        )
