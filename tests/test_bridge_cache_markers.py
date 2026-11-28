# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RS-P12: the bridge carries a harness's cache markers and reports cached tokens (#2208).

One test per acceptance row, tagged with its id. The transport is faked: nothing leaves the process.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from axiom.llm import gateway as gw
from axiom.llm.anthropic_ingress import (
    anthropic_to_gateway,
    completion_to_anthropic,
    completion_to_openai,
    stream_to_anthropic_sse,
    stream_to_openai_sse,
)

EPH = {"type": "ephemeral"}

BODY = {
    "max_tokens": 64,
    "system": [
        {"type": "text", "text": "you are X"},
        {"type": "text", "text": "be terse", "cache_control": EPH},
    ],
    "messages": [
        {"role": "user", "content": [{"type": "text", "text": "hi", "cache_control": EPH}]},
    ],
}


class _Resp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


def _capture(monkeypatch, data):
    seen: dict = {}

    def fake(_requests, url, payload, headers, **_kw):
        seen["url"], seen["payload"] = url, payload
        return _Resp(data)

    monkeypatch.setattr(gw, "_post_with_rate_limit_retry", fake)
    return seen


def _anthropic_provider():
    return gw.LLMProvider(name="a", endpoint="https://api.anthropic.invalid/v1", model="m", api_key_env="X")


def _usage_reply(**usage):
    return {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": usage}


def test_RS_P12_A1_markers_reach_an_anthropic_provider(monkeypatch):
    """A cache_control on a system block and on a message block arrive intact."""
    seen = _capture(monkeypatch, _usage_reply(input_tokens=1, output_tokens=1))
    kw = anthropic_to_gateway(BODY)
    g = gw.Gateway()
    g._call_anthropic_with_tools(_anthropic_provider(), kw["messages"], kw["system"], kw["tools"], kw["max_tokens"])
    payload = seen["payload"]
    assert payload["system"] == [
        {"type": "text", "text": "you are X"},
        {"type": "text", "text": "be terse", "cache_control": EPH},
    ]
    assert payload["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "hi", "cache_control": EPH}]}
    ]


def test_RS_P12_A1_no_markers_leaves_the_payload_as_before(monkeypatch):
    seen = _capture(monkeypatch, _usage_reply(input_tokens=1, output_tokens=1))
    kw = anthropic_to_gateway({"system": "s", "messages": [{"role": "user", "content": "hi"}]})
    gw.Gateway()._call_anthropic_with_tools(
        _anthropic_provider(), kw["messages"], kw["system"], None, 10
    )
    assert seen["payload"]["system"] == "s"
    assert seen["payload"]["messages"] == [{"role": "user", "content": "hi"}]


def test_RS_P12_A2_gateway_reads_cache_write_tokens(monkeypatch):
    _capture(
        monkeypatch,
        _usage_reply(input_tokens=5, output_tokens=2, cache_read_input_tokens=100, cache_creation_input_tokens=40),
    )
    r = gw.Gateway()._call_anthropic_with_tools(
        _anthropic_provider(), [{"role": "user", "content": "x"}], "", None, 10
    )
    assert (r.cache_read_tokens, r.cache_creation_tokens) == (100, 40)


def test_RS_P12_A2_reply_reports_cache_tokens_anthropic_and_openai():
    resp = SimpleNamespace(
        text="ok", tool_use=[], stop_reason="end_turn", input_tokens=5, output_tokens=2,
        cache_read_tokens=100, cache_creation_tokens=40,
    )
    usage = completion_to_anthropic(resp, "m")["usage"]
    assert usage["cache_read_input_tokens"] == 100
    assert usage["cache_creation_input_tokens"] == 40
    oai = completion_to_openai(resp, "m")["usage"]
    assert oai["prompt_tokens_details"]["cached_tokens"] == 100


def test_RS_P12_A2_stream_reports_cache_tokens():
    chunks = [
        SimpleNamespace(type="usage", input_tokens=5, output_tokens=0, cache_read_tokens=100, cache_creation_tokens=40),
        SimpleNamespace(type="text", text="ok"),
        SimpleNamespace(type="usage", input_tokens=0, output_tokens=2, cache_read_tokens=0, cache_creation_tokens=0),
        SimpleNamespace(type="done"),
    ]
    events = "".join(stream_to_anthropic_sse(chunks, "m"))
    delta = [json.loads(line[6:]) for line in events.splitlines() if line.startswith("data: ")]
    usage = [d for d in delta if d["type"] == "message_delta"][0]["usage"]
    assert usage["cache_read_input_tokens"] == 100
    assert usage["cache_creation_input_tokens"] == 40
    assert usage["output_tokens"] == 2
    oai = "".join(stream_to_openai_sse(chunks, "m"))
    last = [json.loads(line[6:]) for line in oai.splitlines() if line.startswith("data: {")][-1]
    assert last["usage"]["prompt_tokens_details"]["cached_tokens"] == 100


def test_RS_P12_A3_markers_are_removed_for_a_provider_that_cannot_keep_them(monkeypatch):
    """An OpenAI-format entry gets no cache_control, and the response records how many were dropped."""
    seen: dict = {}

    def fake_openai(self, provider, messages, system, tools, max_tokens, **_kw):
        seen["messages"] = messages
        return gw.CompletionResponse(text="ok", provider=provider.name)

    monkeypatch.setattr(gw.Gateway, "_call_openai_with_tools", fake_openai)
    kw = anthropic_to_gateway(BODY)
    openai_provider = gw.LLMProvider(name="o", endpoint="https://x.invalid/v1", model="m")
    r = gw.Gateway()._call_provider_with_tools(
        openai_provider, kw["messages"], kw["system"], None, 10
    )
    assert all("cache_control" not in m for m in seen["messages"])
    assert r.cache_markers_dropped == 2  # one on the system block, one on the message
