# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for T0-2 token-aware sliding window.

Replaces the existing char-based trim with a token-aware window that:
    - Always keeps the system block + a summary of dropped history (if any)
    - Keeps a contiguous tail of the most recent messages that fits budget
    - Preserves user/assistant pairing so the model never sees an
      orphan tool_call or orphan user turn without its response
    - Does not mutate the input list

The token-counting function is injected so tests don't need a real
tokenizer and so production callers can plug in tiktoken, Anthropic
``token_count``, or any model-specific counter.
"""

from __future__ import annotations

from collections.abc import Callable

from axiom.infra.token_window import WindowResult, fit_messages

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}


def _const_counter(per_msg: int) -> Callable[[dict], int]:
    """Every message costs exactly ``per_msg`` tokens — easy arithmetic."""
    return lambda _m: per_msg


# ---------------------------------------------------------------------------
# Basic fit
# ---------------------------------------------------------------------------


class TestFitSimple:
    def test_everything_fits(self):
        msgs = [_msg("user", "hi"), _msg("assistant", "hello")]
        result = fit_messages(msgs, max_tokens=100, system_tokens=0,
                               count_fn=_const_counter(10))
        assert isinstance(result, WindowResult)
        assert result.kept == msgs
        assert result.dropped == []
        assert result.tokens_used == 20

    def test_empty_messages(self):
        result = fit_messages([], max_tokens=100, system_tokens=0,
                               count_fn=_const_counter(10))
        assert result.kept == []
        assert result.dropped == []
        assert result.tokens_used == 0


class TestTrimming:
    def test_drops_oldest_first(self):
        msgs = [_msg("user", f"q{i}") for i in range(5)]
        # 5 msgs × 10 tokens = 50; budget 25 → keep last 2 (=20 tokens)
        result = fit_messages(msgs, max_tokens=25, system_tokens=0,
                               count_fn=_const_counter(10))
        assert len(result.kept) == 2
        assert result.kept[0]["content"] == "q3"
        assert result.kept[1]["content"] == "q4"
        assert [m["content"] for m in result.dropped] == ["q0", "q1", "q2"]

    def test_system_tokens_subtracted_from_budget(self):
        msgs = [_msg("user", f"q{i}") for i in range(5)]
        # system_tokens=15, budget 25 → only 10 left → 1 message (10 tok)
        result = fit_messages(msgs, max_tokens=25, system_tokens=15,
                               count_fn=_const_counter(10))
        assert len(result.kept) == 1
        assert result.kept[0]["content"] == "q4"

    def test_budget_smaller_than_one_message_keeps_latest(self):
        """When even the latest message can't fit cleanly, we still keep
        it — the alternative (empty kept list) is useless for chat."""
        msgs = [_msg("user", "q0"), _msg("user", "q1")]
        result = fit_messages(msgs, max_tokens=5, system_tokens=0,
                               count_fn=_const_counter(10))
        assert len(result.kept) == 1
        assert result.kept[0]["content"] == "q1"


class TestPairingPreservation:
    def test_never_keep_orphan_tool_result(self):
        """A ``tool`` message without its preceding assistant tool_call
        is invalid — the window must drop it too."""
        msgs = [
            _msg("user", "q"),
            {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
            {"role": "tool", "tool_call_id": "t1", "content": "result"},
            _msg("assistant", "done"),
        ]
        # Force tight budget: only last 2 messages fit at 10 tok each.
        # Naive window would keep tool + assistant but drop the
        # assistant-with-tool_call that spawned the tool message.
        result = fit_messages(msgs, max_tokens=20, system_tokens=0,
                               count_fn=_const_counter(10))
        roles = [m["role"] for m in result.kept]
        if "tool" in roles:
            # Must also include the paired assistant with tool_calls.
            assert any(
                m["role"] == "assistant" and m.get("tool_calls")
                for m in result.kept
            )

    def test_keeps_user_without_orphan_assistant(self):
        """A trailing user message with no assistant reply yet is fine
        (we're about to generate one); this is the normal pre-turn state."""
        msgs = [_msg("user", "q0"), _msg("assistant", "a0"), _msg("user", "q1")]
        result = fit_messages(msgs, max_tokens=20, system_tokens=0,
                               count_fn=_const_counter(10))
        # Last 2 fit: [assistant a0, user q1]
        assert len(result.kept) == 2
        assert result.kept[-1]["content"] == "q1"


class TestImmutability:
    def test_input_not_mutated(self):
        msgs = [_msg("user", "q0"), _msg("user", "q1")]
        original = [dict(m) for m in msgs]
        fit_messages(msgs, max_tokens=10, system_tokens=0,
                     count_fn=_const_counter(10))
        assert msgs == original


class TestWindowResult:
    def test_result_exposes_drop_summary_seed(self):
        """WindowResult includes enough info for the summarizer to work
        with the dropped messages (roles, content, tool_calls)."""
        msgs = [_msg("user", f"q{i}") for i in range(5)]
        result = fit_messages(msgs, max_tokens=25, system_tokens=0,
                               count_fn=_const_counter(10))
        assert result.dropped_count == 3
        assert result.dropped[0]["role"] == "user"
        assert result.tokens_used + result.tokens_dropped == 50
