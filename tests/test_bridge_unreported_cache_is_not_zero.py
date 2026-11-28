# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A server that reported no cache use is not a server that cached nothing (ADR-272 D6).

The gateway records ``cache_reported=False`` for a reply that said nothing
about its cache. The bridge used to hand its client ``cached_tokens: 0`` (and
the Anthropic cache fields at 0) for such a reply, so a client's hit ratio
counted a miss that nobody measured. The bridge now leaves the cache fields
out, and keeps a reported 0 as 0.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from axiom.llm.anthropic_ingress import (
    completion_to_anthropic,
    completion_to_openai,
    stream_to_anthropic_sse,
    stream_to_openai_sse,
)
from axiom.llm.gateway import CompletionResponse, StreamChunk


def _reply(**kw) -> CompletionResponse:
    return CompletionResponse(text="ok", input_tokens=5, output_tokens=2, **kw)


def _openai_last(chunks) -> dict:
    out = "".join(stream_to_openai_sse(chunks, "m"))
    return [json.loads(e[6:]) for e in out.split("\n\n") if e.startswith("data: {")][-1]


def _anthropic_delta_usage(chunks) -> dict:
    out = "".join(stream_to_anthropic_sse(chunks, "m"))
    for event in out.split("\n\n"):
        if event.startswith("event: message_delta"):
            return json.loads(event.split("data: ", 1)[1])["usage"]
    raise AssertionError("no message_delta event")


def _stream(*usage: StreamChunk) -> list:
    return [*usage, StreamChunk(type="text", text="ok"), StreamChunk(type="done")]


def test_an_openai_reply_with_no_cache_report_carries_no_cached_count():
    usage = completion_to_openai(_reply(), "m")["usage"]
    assert usage["prompt_tokens"] == 5
    assert usage["completion_tokens"] == 2
    assert "prompt_tokens_details" not in usage


def test_an_openai_reply_that_reported_zero_says_zero():
    usage = completion_to_openai(_reply(cache_reported=True), "m")["usage"]
    assert usage["prompt_tokens_details"] == {"cached_tokens": 0}


def test_an_openai_stream_with_no_cache_report_carries_no_cached_count():
    usage = _openai_last(_stream(StreamChunk(type="usage", input_tokens=5, output_tokens=2)))[
        "usage"
    ]
    assert usage["prompt_tokens"] == 5
    assert usage["total_tokens"] == 7
    assert "prompt_tokens_details" not in usage


def test_an_openai_stream_reports_the_cache_when_any_chunk_did():
    chunks = _stream(
        StreamChunk(type="usage", input_tokens=5, cache_reported=True),
        StreamChunk(type="usage", output_tokens=2),
    )
    assert _openai_last(chunks)["usage"]["prompt_tokens_details"] == {"cached_tokens": 0}


def test_an_anthropic_reply_with_no_cache_report_carries_no_cache_fields():
    usage = completion_to_anthropic(_reply(), "m")["usage"]
    assert usage == {"input_tokens": 5, "output_tokens": 2}


def test_an_anthropic_reply_that_reported_zero_says_zero():
    usage = completion_to_anthropic(_reply(cache_reported=True), "m")["usage"]
    assert usage["cache_read_input_tokens"] == 0
    assert usage["cache_creation_input_tokens"] == 0


def test_an_anthropic_stream_with_no_cache_report_carries_no_cache_fields():
    usage = _anthropic_delta_usage(_stream(StreamChunk(type="usage", output_tokens=2)))
    assert usage == {"output_tokens": 2}


def test_an_anthropic_stream_that_reported_zero_says_zero():
    usage = _anthropic_delta_usage(
        _stream(StreamChunk(type="usage", output_tokens=2, cache_reported=True))
    )
    assert usage == {
        "output_tokens": 2,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }


def test_a_count_above_zero_is_a_report_even_without_the_flag():
    # A reply built by an older caller that never set the flag still carries its numbers.
    reply = SimpleNamespace(
        text="ok",
        input_tokens=5,
        output_tokens=2,
        cache_read_tokens=100,
        cache_creation_tokens=0,
        tool_use=[],
        stop_reason="end_turn",
    )
    assert completion_to_openai(reply, "m")["usage"]["prompt_tokens_details"] == {
        "cached_tokens": 100
    }
    assert completion_to_anthropic(reply, "m")["usage"]["cache_read_input_tokens"] == 100
