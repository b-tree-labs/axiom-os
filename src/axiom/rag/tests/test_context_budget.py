# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for context-budget helpers (overflow detection + request trimming)."""

from __future__ import annotations

from axiom.rag.context_budget import (
    cap_text_to_tokens,
    estimate_messages_tokens,
    is_context_overflow,
    trim_messages_to_budget,
)


def test_detects_real_llama_cpp_overflow():
    # The exact string the node's backend emitted.
    err = (
        "request (46228 tokens) exceeds the available context size "
        "(43776 tokens), try increasing it"
    )
    assert is_context_overflow(err)


def test_detects_openai_style_overflow():
    assert is_context_overflow("This model's maximum context length is 8192 tokens...")
    assert is_context_overflow("Please reduce the length of the messages.")


def test_ignores_unrelated_errors():
    assert not is_context_overflow("500 internal server error")
    assert not is_context_overflow("")
    assert not is_context_overflow(None)


def test_cap_text_truncates_and_marks():
    text = "x" * 1000
    out = cap_text_to_tokens(text, 10)  # ~40 chars
    assert len(out) < len(text)
    assert out.endswith("…[truncated]")
    assert cap_text_to_tokens("short", 100) == "short"
    assert cap_text_to_tokens("anything", 0) == ""


def _msg(role, content):
    return {"role": role, "content": content}


def test_trim_drops_oldest_keeps_system_and_tail():
    msgs = [
        _msg("system", "S" * 40),
        _msg("user", "U1" * 200),      # oldest droppable
        _msg("assistant", "A1" * 200),
        _msg("user", "U2" * 200),
        _msg("assistant", "A2" * 200),  # tail (keep_last=2 -> last two kept)
    ]
    trimmed, dropped = trim_messages_to_budget(msgs, budget_tokens=200, keep_last=2)
    assert dropped >= 1
    assert trimmed[0]["role"] == "system"          # system preserved
    assert trimmed[-1]["content"] == msgs[-1]["content"]  # newest kept
    assert trimmed[-2]["content"] == msgs[-2]["content"]
    # the dropped ones came off the front (after system)
    assert msgs[1] not in trimmed


def test_trim_floor_never_drops_system_or_last_n():
    msgs = [_msg("system", "S" * 4000), _msg("user", "U" * 4000), _msg("assistant", "A" * 4000)]
    trimmed, dropped = trim_messages_to_budget(msgs, budget_tokens=1, keep_last=2)
    # system + last 2 == all three here; nothing droppable
    assert len(trimmed) == 3
    assert dropped == 0


def test_trim_noop_when_already_under_budget():
    msgs = [_msg("user", "hi"), _msg("assistant", "hello")]
    trimmed, dropped = trim_messages_to_budget(msgs, budget_tokens=10_000)
    assert dropped == 0
    assert trimmed == list(msgs)


def test_estimate_scales_with_size():
    small = [_msg("user", "hi")]
    big = [_msg("user", "word " * 1000)]
    assert estimate_messages_tokens(big) > estimate_messages_tokens(small)
