# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A request too large for the model's context window is said plainly, never answered without its tools.

Found live on a fresh install: the default local model has a 4,096-token window and the chat's tool
definitions alone are larger than that, so the server refused every request. The non-streaming path then
retried WITHOUT the tools (a fallback meant for servers that reject the ``tools`` parameter), the smaller
request fit, and the model answered a question about the person's data with no way to read it, in a voice
that cited tools it never called. The streaming path said only "LLM stream failed ... HTTP 400".
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
import requests

from axiom.llm import gateway as gw

_REFUSAL = json.dumps(
    {
        "error": {
            "code": 400,
            "message": "the request exceeds the available context size, try increasing it",
            "type": "exceed_context_size_error",
            "n_prompt_tokens": 12932,
            "n_ctx": 4096,
        }
    }
)
_INVENTED = "Per the latest reading [C1], the value is 950."
_TOOLS = [{"type": "function", "function": {"name": "data_latest", "description": "x" * 400, "parameters": {}}}]


def _http_error(status: int, body: str) -> requests.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    resp._content = body.encode()
    return requests.HTTPError(f"{status} Client Error", response=resp)


class _Ok:
    def json(self):
        return {"choices": [{"message": {"content": _INVENTED}, "finish_reason": "stop"}], "usage": {}}


def _provider(**kw):
    return gw.LLMProvider(
        name="local", endpoint="http://127.0.0.1:1", model="tiny-1b", api_key_env="", **kw
    )


def _gateway(provider):
    with patch.object(gw.Gateway, "_load_config", lambda self: None):
        g = gw.Gateway()
    g.providers = [provider]
    g._write_routing_audit = lambda **k: None
    return g


@pytest.fixture
def posts(monkeypatch):
    """A server that refuses any request carrying tools as too large, and answers any request without."""
    sent: list[dict] = []

    def post(_requests, url, payload, headers, timeout=60, **kw):
        sent.append(dict(payload))  # the gateway mutates its payload between tries
        if payload.get("tools"):
            raise _http_error(400, _REFUSAL)
        return _Ok()

    monkeypatch.setattr(gw, "_post_with_rate_limit_retry", post)
    return sent


def test_the_tools_are_not_stripped_to_make_it_fit(posts):
    g = _gateway(_provider())
    out = g.complete_with_tools([{"role": "user", "content": "what is the value?"}], tools=_TOOLS)
    assert [bool(p.get("tools")) for p in posts] == [True], "retried without tools"
    assert _INVENTED not in out.text
    assert out.success is False
    assert out.error == gw.CONTEXT_WINDOW_EXCEEDED


def test_the_message_names_the_model_its_window_the_size_and_the_fix(posts):
    g = _gateway(_provider())
    text = g.complete_with_tools([{"role": "user", "content": "q"}], tools=_TOOLS).text
    assert "tiny-1b" in text
    assert "4,096" in text and "12,932" in text
    assert "tool definitions" in text
    assert "connect list" in text and "connect <preset>" in text


def test_a_configured_window_is_used_when_the_server_does_not_state_one(monkeypatch):
    def post(_requests, url, payload, headers, timeout=60, **kw):
        raise _http_error(400, '{"error":{"message":"context_length_exceeded"}}')

    monkeypatch.setattr(gw, "_post_with_rate_limit_retry", post)
    g = _gateway(_provider(context_window=2048))
    text = g.complete_with_tools([{"role": "user", "content": "q"}], tools=_TOOLS).text
    assert "2,048" in text and "connect list" in text


def test_a_server_that_rejects_the_tools_parameter_still_gets_the_tool_less_retry(monkeypatch):
    """The fallback the context case was wrongly taking still exists for what it was for."""
    sent: list[dict] = []

    def post(_requests, url, payload, headers, timeout=60, **kw):
        sent.append(dict(payload))  # the gateway mutates its payload between tries
        if payload.get("tools"):
            raise _http_error(400, '{"error":"tools are not supported"}')
        return _Ok()

    monkeypatch.setattr(gw, "_post_with_rate_limit_retry", post)
    out = _gateway(_provider()).complete_with_tools([{"role": "user", "content": "q"}], tools=_TOOLS)
    assert [bool(p.get("tools")) for p in sent] == [True, False]
    assert out.success is True


def test_the_stream_says_it_plainly_when_every_model_is_too_small():
    g = gw.Gateway.__new__(gw.Gateway)
    provider = _provider()
    g.providers = [provider]
    g._trace_call = lambda *a, **k: None
    g._trace_generation = lambda *a, **k: None
    g._select_provider = lambda task, routing_tier, prefer=None: provider
    g._check_vpn = lambda p: True

    def stream(p, *args):
        raise gw.PersistentLLMError("provider rejected request", provider=p.name, status=400, body=_REFUSAL)
        yield  # pragma: no cover

    g._stream_provider = stream
    text = "".join(
        c.text for c in g.stream_with_tools([{"role": "user", "content": "q"}], tools=_TOOLS) if c.type == "text"
    )
    assert "LLM stream failed" not in text
    assert "tiny-1b" in text and "4,096" in text and "12,932" in text and "connect list" in text
