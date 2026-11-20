# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for T0-2 conversation window composer.

Ties together ``fit_messages`` (window), ``TokenCounter`` (sizing), and
a summarizer hook. Responsibility: given raw session messages and a
budget, return the final message list to send to the model, with a
summary of dropped history injected so nothing is silently lost.

Summarization strategy for v1 is deterministic (count + role roll-up).
The summarizer callable is pluggable so an LLM-based summarizer can be
dropped in without changing callers.
"""

from __future__ import annotations

from axiom.infra.conversation_window import (
    build_window,
    default_summarizer,
)


def _msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}


# ---------------------------------------------------------------------------
# Default (deterministic) summarizer
# ---------------------------------------------------------------------------


class TestDefaultSummarizer:
    def test_empty_dropped_returns_empty(self):
        assert default_summarizer([]) == ""

    def test_summary_mentions_count(self):
        dropped = [_msg("user", "q"), _msg("assistant", "a")]
        summary = default_summarizer(dropped)
        assert "2" in summary

    def test_summary_rolls_up_roles(self):
        dropped = [_msg("user", f"q{i}") for i in range(3)] + [
            _msg("assistant", f"a{i}") for i in range(3)
        ]
        summary = default_summarizer(dropped)
        assert "user" in summary.lower()
        assert "assistant" in summary.lower()


# ---------------------------------------------------------------------------
# build_window
# ---------------------------------------------------------------------------


class TestBuildWindow:
    def test_all_fits_returns_unchanged(self):
        msgs = [_msg("user", "q"), _msg("assistant", "a")]
        out = build_window(msgs, max_tokens=1000, system_tokens=0)
        assert out == msgs

    def test_over_budget_drops_oldest_and_injects_summary(self):
        # 10 messages; tight budget forces drops.
        msgs = [_msg("user", f"q{i}" * 50) for i in range(10)]
        out = build_window(msgs, max_tokens=200, system_tokens=0)
        # First message in output must be the summary system message
        assert out[0]["role"] == "system"
        assert "omitted" in out[0]["content"].lower() or "earlier" in out[0]["content"].lower()

    def test_summary_only_injected_when_drops_occurred(self):
        msgs = [_msg("user", "q1"), _msg("user", "q2")]
        out = build_window(msgs, max_tokens=1000, system_tokens=0)
        # No drops → no summary
        assert out[0]["role"] == "user"

    def test_summarizer_callable_is_pluggable(self):
        msgs = [_msg("user", f"q{i}" * 50) for i in range(10)]
        called = []

        def custom_summary(dropped):
            called.append(len(dropped))
            return "CUSTOM-SUMMARY"

        out = build_window(
            msgs, max_tokens=200, system_tokens=0, summarizer=custom_summary
        )
        assert called  # custom summarizer was invoked
        assert out[0]["content"] == "CUSTOM-SUMMARY"

    def test_preserves_oldest_to_newest_order(self):
        msgs = [_msg("user", f"q{i}") for i in range(5)]
        out = build_window(msgs, max_tokens=1000, system_tokens=0)
        assert [m["content"] for m in out] == ["q0", "q1", "q2", "q3", "q4"]


class TestIntegrationWithRealCounter:
    """End-to-end via the default token counter (tiktoken or fallback)."""

    def test_short_conversation_fits_small_budget(self):
        msgs = [_msg("user", "hi"), _msg("assistant", "hello")]
        out = build_window(msgs, max_tokens=100, system_tokens=0)
        assert len(out) == 2

    def test_long_conversation_drops_with_summary(self):
        # ~1000 tokens of content; budget 100 → must drop.
        msgs = [
            _msg("user", "This is a long message repeated many times. " * 20)
            for _ in range(10)
        ]
        out = build_window(msgs, max_tokens=100, system_tokens=0)
        assert out[0]["role"] == "system"
        # Final kept message is the most recent
        assert out[-1]["content"].startswith("This is a long message")
