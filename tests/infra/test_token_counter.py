# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for T0-2 token counter.

Real token counting (tiktoken if installed; otherwise a calibrated
word-based fallback). Used by the sliding window to decide what fits.
"""

from __future__ import annotations

from axiom.infra.token_counter import (
    TokenCounter,
    count_message,
    count_tokens,
    fallback_count,
)


class TestFallbackHeuristic:
    def test_empty_string_is_zero(self):
        assert fallback_count("") == 0

    def test_single_word_is_one_token(self):
        assert fallback_count("quantum") >= 1

    def test_roughly_linear_in_length(self):
        short = fallback_count("the quick brown fox")
        long = fallback_count("the quick brown fox " * 100)
        assert long > short * 50  # near-linear (allows some slop)

    def test_punctuation_does_not_explode_counts(self):
        """Pathological punctuation shouldn't inflate count 20×."""
        normal = fallback_count("hello world")
        punct = fallback_count("hello, world. ! ? -- -- --")
        assert punct < normal * 20


class TestPublicCountTokens:
    def test_returns_non_negative(self):
        assert count_tokens("") == 0
        assert count_tokens("quantum mechanics") >= 1


class TestCountMessage:
    def test_includes_overhead(self):
        """Per-message overhead ensures role + formatting count."""
        content_only = count_tokens("hello")
        whole_msg = count_message({"role": "user", "content": "hello"})
        assert whole_msg > content_only  # overhead added

    def test_counts_tool_calls(self):
        msg = {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "t1", "name": "search", "input": {"q": "x"}}],
        }
        assert count_message(msg) > 0

    def test_empty_content_returns_overhead(self):
        """Bare 'role' with empty content still has the per-message overhead."""
        n = count_message({"role": "assistant", "content": ""})
        assert n >= 1


class TestTokenCounterClass:
    def test_default_counter_works(self):
        tc = TokenCounter()
        assert tc.count("hello world") >= 1

    def test_pluggable_backend(self):
        """A custom backend can be injected for deterministic tests."""
        tc = TokenCounter(backend=lambda s: len(s))  # char-count
        assert tc.count("hello") == 5
