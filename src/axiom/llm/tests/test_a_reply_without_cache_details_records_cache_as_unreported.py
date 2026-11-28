# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A server that reports no cached-token field is recorded as unreported, never as 0 (RS-P11-A3, ADR-272 D6)."""

from __future__ import annotations

from tests.routing_acceptance.test_p0 import _record_posts, _Sse, _usage_gateway

_USER = [{"role": "user", "content": "hi"}]


def _gateway(monkeypatch):
    return _usage_gateway(
        monkeypatch, endpoint="http://llm.example.org:8000/v1", model="local-test"
    )


def test_a_non_streamed_openai_reply_without_prompt_tokens_details_is_unreported(monkeypatch):
    gw = _gateway(monkeypatch)
    body = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 300, "completion_tokens": 20},
    }
    _record_posts(monkeypatch, lambda p: _Sse([], body=body))

    resp = gw._call_openai_with_tools(gw.providers[0], _USER, "", None, 100)

    assert resp.cache_reported is False


def test_a_streamed_openai_turn_without_prompt_tokens_details_is_unreported(monkeypatch):
    gw = _gateway(monkeypatch)
    events = [
        {"choices": [{"index": 0, "delta": {"content": "an answer"}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 300, "completion_tokens": 20}},
    ]
    _record_posts(monkeypatch, lambda p: _Sse(events))

    chunks = list(gw._stream_openai(gw.providers[0], _USER, "", None, 100))

    usage = [c for c in chunks if c.type == "usage"]
    assert usage and not any(c.cache_reported for c in usage)


def test_a_reply_that_reports_zero_cached_tokens_is_reported_not_unreported(monkeypatch):
    gw = _gateway(monkeypatch)
    body = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 300,
            "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    }
    _record_posts(monkeypatch, lambda p: _Sse([], body=body))

    resp = gw._call_openai_with_tools(gw.providers[0], _USER, "", None, 100)

    assert resp.cache_reported is True
    assert resp.cache_read_tokens == 0


def test_an_anthropic_reply_without_cache_fields_is_unreported(monkeypatch):
    gw = _usage_gateway(monkeypatch, endpoint="https://api.anthropic.com/v1", model="claude-test")
    body = {
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 12, "output_tokens": 7},
    }
    _record_posts(monkeypatch, lambda p: _Sse([], body=body))

    resp = gw._call_anthropic_with_tools(gw.providers[0], _USER, "", None, 100)

    assert resp.cache_reported is False
