# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Cache writes are counted from every reply shape the gateway reads (RS-P11-A1, A2, ADR-272 D6).

The Anthropic format reports writes as ``cache_creation_input_tokens`` (and, per lifetime, in
``cache_creation.ephemeral_5m_input_tokens`` / ``ephemeral_1h_input_tokens``). OpenRouter reports them
as ``prompt_tokens_details.cache_write_tokens`` on the OpenAI format. The gateway keeps its field name
``cache_creation_tokens`` for the number.
"""

from __future__ import annotations

from tests.routing_acceptance.test_p0 import _record_posts, _Sse, _usage_gateway

_USER = [{"role": "user", "content": "hi"}]


def _anthropic_gateway(monkeypatch):
    return _usage_gateway(monkeypatch, endpoint="https://api.anthropic.com/v1", model="claude-test")


def _openai_gateway(monkeypatch):
    return _usage_gateway(
        monkeypatch, endpoint="https://openrouter.example/v1", model="vendor/model"
    )


def _summed(chunks, name):
    return sum(getattr(c, name) for c in chunks if c.type == "usage")


def test_an_anthropic_reply_reports_its_cache_writes_by_lifetime(monkeypatch):
    gw = _anthropic_gateway(monkeypatch)
    usage = {
        "input_tokens": 12,
        "output_tokens": 7,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 300,
        "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 200},
    }
    body = {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": usage}
    _record_posts(monkeypatch, lambda _p: _Sse([], body=body))

    resp = gw._call_anthropic_with_tools(gw.providers[0], _USER, "", None, 100)

    assert resp.cache_creation_tokens == 300
    assert (resp.cache_write_5m_tokens, resp.cache_write_1h_tokens) == (100, 200)
    assert resp.cache_reported is True
    assert resp.input_includes_cache is False


def test_a_streamed_anthropic_turn_counts_its_cache_writes_once(monkeypatch):
    gw = _anthropic_gateway(monkeypatch)
    split = {"ephemeral_5m_input_tokens": 40, "ephemeral_1h_input_tokens": 0}
    start = {
        "input_tokens": 12,
        "output_tokens": 1,
        "cache_read_input_tokens": 900,
        "cache_creation_input_tokens": 40,
        "cache_creation": split,
    }
    cumulative = start | {"output_tokens": 7}
    events = [
        {"type": "message_start", "message": {"usage": start}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": cumulative},
        {"type": "message_stop"},
    ]
    _record_posts(monkeypatch, lambda _p: _Sse(events, done=False))

    chunks = list(gw._stream_anthropic(gw.providers[0], _USER, "", None, 100))

    assert _summed(chunks, "cache_creation_tokens") == 40
    assert _summed(chunks, "cache_write_5m_tokens") == 40
    assert _summed(chunks, "cache_write_1h_tokens") == 0
    assert _summed(chunks, "cache_read_tokens") == 900


def test_a_streamed_anthropic_turn_with_no_cache_fields_is_unreported(monkeypatch):
    gw = _anthropic_gateway(monkeypatch)
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 12, "output_tokens": 1}}},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 7},
        },
        {"type": "message_stop"},
    ]
    _record_posts(monkeypatch, lambda _p: _Sse(events, done=False))

    chunks = list(gw._stream_anthropic(gw.providers[0], _USER, "", None, 100))

    assert not any(c.cache_reported for c in chunks if c.type == "usage")


def test_an_openrouter_reply_reports_its_cache_writes(monkeypatch):
    gw = _openai_gateway(monkeypatch)
    details = {"cached_tokens": 256, "cache_write_tokens": 64}
    body = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 300, "completion_tokens": 20, "prompt_tokens_details": details},
    }
    _record_posts(monkeypatch, lambda p: _Sse([], body=body))

    resp = gw._call_openai_with_tools(gw.providers[0], _USER, "", None, 100)

    assert (resp.cache_read_tokens, resp.cache_creation_tokens) == (256, 64)
    assert resp.cache_reported is True
    assert resp.input_includes_cache is True


def test_a_streamed_openrouter_turn_reports_its_cache_writes(monkeypatch):
    gw = _openai_gateway(monkeypatch)
    details = {"cached_tokens": 256, "cache_write_tokens": 64}
    events = [
        {"choices": [{"index": 0, "delta": {"content": "an answer"}, "finish_reason": "stop"}]},
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 300,
                "completion_tokens": 20,
                "prompt_tokens_details": details,
            },
        },
    ]
    _record_posts(monkeypatch, lambda p: _Sse(events))

    chunks = list(gw._stream_openai(gw.providers[0], _USER, "", None, 100))

    assert _summed(chunks, "cache_creation_tokens") == 64
    assert _summed(chunks, "cache_read_tokens") == 256
    assert all(c.input_includes_cache and c.cache_reported for c in chunks if c.type == "usage")
