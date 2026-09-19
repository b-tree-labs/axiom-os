# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Inline `<think>` is reasoning, not an answer, and must never become history.

Reasoning models report chain-of-thought two different ways. Some return it in
a separate `reasoning_content` field, which the gateway already handles. Others
— Qwen3 through an OpenAI-compatible server, among them — return it inline at
the head of `content`.

Nothing stripped the inline form, so the whole block became the assistant's
answer text, was stored as a conversation message, and was replayed on the next
round. Observed against a real node: asked to list telemetry metrics, the model
reasoned correctly ("there's one called telemetry_metrics"), that reasoning was
captured as the reply, the tool call never materialised, and the assistant then
told the user the tool "is not available in your current environment" — arguing
against a fact it had just established, because it was reading its own
unfinished thinking as conversation history.
"""

from __future__ import annotations

from axiom.llm.reasoning import InlineReasoningStream, split_inline_reasoning


def test_inline_reasoning_is_separated_from_the_answer():
    text = "<think>\nThe user wants metrics. I should call the tool.\n</think>\nHere are the metrics."

    answer, reasoning = split_inline_reasoning(text)

    assert answer == "Here are the metrics."
    assert "I should call the tool." in reasoning


def test_an_unclosed_think_block_is_all_reasoning():
    """Truncation mid-thought: there is no answer yet, and pretending the
    reasoning is one is how the model ends up arguing with itself later."""
    text = "<think>\nLooking at the functions, there's one called telemetry_metrics"

    answer, reasoning = split_inline_reasoning(text)

    assert answer == ""
    assert "telemetry_metrics" in reasoning


def test_ordinary_text_is_untouched():
    """Negative control: this must not rewrite normal answers."""
    text = "The metrics are Shim1, Reg and FuelTemp1."

    answer, reasoning = split_inline_reasoning(text)

    assert answer == text
    assert reasoning == ""


def test_prose_mentioning_thinking_is_not_a_reasoning_block():
    text = "I was thinking about <think> tags as a concept."

    answer, _ = split_inline_reasoning(text)

    assert answer == text


def test_several_blocks_are_all_removed():
    text = "<think>a</think>One.<think>b</think>Two."

    answer, reasoning = split_inline_reasoning(text)

    assert answer == "One.Two."
    assert "a" in reasoning and "b" in reasoning


def test_empty_input():
    assert split_inline_reasoning("") == ("", "")
    assert split_inline_reasoning(None) == ("", "")


# --- streaming: the tags arrive split across deltas -------------------------
#
# The non-streaming fix does not help a surface that streams, and the serving
# and headless paths both stream. A tag can be split across chunk boundaries,
# so this cannot be done with a per-chunk regex.


def _run(chunks):
    s = InlineReasoningStream()
    text, thinking = [], []
    for c in chunks:
        t, th = s.feed(c)
        text.append(t)
        thinking.append(th)
    t, th = s.flush()
    text.append(t)
    thinking.append(th)
    return "".join(text), "".join(thinking)


def test_streaming_separates_a_whole_block():
    text, thinking = _run(["<think>", "reasoning here", "</think>", "The answer."])

    assert text == "The answer."
    assert "reasoning here" in thinking


def test_streaming_handles_a_tag_split_across_chunks():
    """The case a per-chunk regex cannot see."""
    text, thinking = _run(["<thi", "nk>hidden</thi", "nk>visible"])

    assert text == "visible"
    assert "hidden" in thinking


def test_streaming_leaves_ordinary_text_alone():
    """Negative control: no tags, nothing withheld."""
    text, thinking = _run(["Shim1, ", "Reg and ", "FuelTemp1."])

    assert text == "Shim1, Reg and FuelTemp1."
    assert thinking == ""


def test_streaming_never_loses_characters_to_the_lookahead_buffer():
    """A partial-tag buffer that forgets to flush silently truncates answers."""
    text, _ = _run(["answer ending in a bracket <"])

    assert text == "answer ending in a bracket <"


def test_streaming_unclosed_block_yields_no_answer():
    text, thinking = _run(["<think>cut off mid-thought"])

    assert text == ""
    assert "cut off mid-thought" in thinking
