# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for #83 LLM-based conversation-history summarizer.

T0-2's conversation window already accepts a pluggable summarizer.
This module builds a concrete LLM-backed one using structured_output
so the generated summary is schema-validated (no regex parsing of
free-form model output).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from axiom.infra.conversation_window import build_window
from axiom.infra.gateway import CompletionResponse, ToolUseBlock
from axiom.infra.llm_summarizer import build_llm_summarizer


def _gateway_returning(summary_text: str) -> MagicMock:
    gw = MagicMock()
    gw.complete_with_tools.return_value = CompletionResponse(
        tool_use=[ToolUseBlock(
            tool_id="t1", name="emit_summary",
            input={"summary": summary_text},
        )],
        success=True,
    )
    return gw


class TestLLMSummarizerBasic:
    def test_returns_summary_string(self):
        gw = _gateway_returning("The user asked three questions about reactor safety; assistant answered with decay-heat curves.")
        summarizer = build_llm_summarizer(gw)
        dropped = [
            {"role": "user", "content": "what is decay heat"},
            {"role": "assistant", "content": "Decay heat is..."},
        ]
        out = summarizer(dropped)
        assert "decay" in out.lower()
        assert "[earlier context" in out  # Wrapped to match default format

    def test_empty_dropped_returns_empty(self):
        """Don't call the gateway when there's nothing to summarize."""
        gw = MagicMock()
        summarizer = build_llm_summarizer(gw)
        assert summarizer([]) == ""
        gw.complete_with_tools.assert_not_called()


class TestGatewayShape:
    def test_calls_complete_with_tools_with_emit_summary(self):
        gw = _gateway_returning("ok")
        summarizer = build_llm_summarizer(gw)
        summarizer([{"role": "user", "content": "q"}])
        kwargs = gw.complete_with_tools.call_args.kwargs
        assert len(kwargs["tools"]) == 1
        assert kwargs["tools"][0]["name"] == "emit_summary"

    def test_dropped_transcript_reaches_prompt(self):
        gw = _gateway_returning("ok")
        summarizer = build_llm_summarizer(gw)
        summarizer([
            {"role": "user", "content": "DISTINCT_USER_MARKER"},
            {"role": "assistant", "content": "DISTINCT_ASST_MARKER"},
        ])
        prompt = gw.complete_with_tools.call_args.kwargs["messages"][0]["content"]
        assert "DISTINCT_USER_MARKER" in prompt
        assert "DISTINCT_ASST_MARKER" in prompt


class TestFallbackOnFailure:
    def test_schema_validation_error_returns_deterministic_fallback(self):
        """If the LLM call fails (stub gateway, no tools), fall back to
        deterministic summary so the conversation window still works."""
        gw = MagicMock()
        gw.complete_with_tools.return_value = CompletionResponse(
            tool_use=[], success=False, error="no provider",
        )
        summarizer = build_llm_summarizer(gw)
        out = summarizer([
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
        ])
        # Fallback format matches default_summarizer shape.
        assert "2 messages omitted" in out


class TestIntegrationWithBuildWindow:
    def test_plugs_into_build_window(self):
        """build_window accepts the LLM summarizer without code changes."""
        gw = _gateway_returning("Summary of dropped turns.")
        summarizer = build_llm_summarizer(gw)
        msgs = [
            {"role": "user", "content": "A" * 500} for _ in range(20)
        ]
        out = build_window(
            msgs, max_tokens=100, system_tokens=0, summarizer=summarizer,
        )
        # When trimmed, the head is a system message with our summary.
        assert out[0]["role"] == "system"
        assert "Summary of dropped turns" in out[0]["content"]
