# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A reasoning effort reaches the request in the form the provider's server takes, and only when it takes one.

The gateway had no reasoning-effort setting, so reasoning models always ran at their server's default and a
caller's effort (Open WebUI's "Reasoning Effort", a document parser's ``reasoning_effort``) was dropped. Servers take it
differently: OpenAI's gpt-5, gpt-6 and o-series and Ollama take ``reasoning_effort`` (on Ollama 0.34, ``none``
turned Qwen 3.6's thinking off: 3 s instead of 65 s for the same answer); llama-server and vLLM switch a Qwen-style
model's thinking with ``chat_template_kwargs.enable_thinking``. A model that takes no effort field gets none, because
an unknown field can fail the request, as ``max_tokens`` does for gpt-5.
"""

from __future__ import annotations

import inspect

import pytest

from axiom.llm import gateway as gw


class _Response:
    status_code = 200

    def iter_lines(self, decode_unicode=True):
        yield 'data: {"choices":[{"delta":{"content":"ok"}}]}'
        yield "data: [DONE]"

    def json(self):
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}


def _capture(monkeypatch):
    seen: dict = {}

    def post(requests_mod, url, payload, headers, timeout=60, **kwargs):
        seen["payload"] = payload
        return _Response()

    monkeypatch.setattr(gw, "_post_with_rate_limit_retry", post)
    return seen


def _provider(model="m", reasoning_param="", reasoning_effort=""):
    return gw.LLMProvider(name="p", endpoint="http://localhost:11434/v1", model=model, api_key_env="",
                          reasoning_param=reasoning_param, reasoning_effort=reasoning_effort)


MSGS = [{"role": "user", "content": "hi"}]
G = gw.Gateway.__new__(gw.Gateway)


def _tool_call(p, effort):
    G._call_provider_with_tools(p, MSGS, "", None, 64, reasoning_effort=effort)


def _stream(p, effort):
    list(G._stream_provider(p, MSGS, "", None, 64, reasoning_effort=effort))


CASES = [  # (provider, per-call effort, fields expected in the payload)
    (_provider(model="gpt-5.4"), "low", {"reasoning_effort": "low"}),  # derived from the model name
    (_provider(model="@slug/gpt-6-luna"), "high", {"reasoning_effort": "high"}),
    (_provider(model="qwen3.6:35b-16k-t0"), "low", {}),  # takes none unless the provider says so
    (_provider(model="qwen3.6:35b-16k-t0", reasoning_param="reasoning_effort"), "none", {"reasoning_effort": "none"}),
    (_provider(model="qwen3.6:35b", reasoning_param="chat_template_kwargs"), "none",
     {"chat_template_kwargs": {"enable_thinking": False}}),
    (_provider(model="qwen3.6:35b", reasoning_param="chat_template_kwargs"), "medium",
     {"chat_template_kwargs": {"enable_thinking": True}}),
    (_provider(model="gpt-5.4", reasoning_param="none"), "low", {}),  # a server that rejects the field
    (_provider(model="gpt-5.4", reasoning_effort="minimal"), None, {"reasoning_effort": "minimal"}),  # provider default
    (_provider(model="gpt-5.4", reasoning_effort="minimal"), "high", {"reasoning_effort": "high"}),  # the call wins
    (_provider(model="gpt-5.4"), None, {}),  # nothing set, nothing sent
]
FIELDS = ("reasoning_effort", "chat_template_kwargs")


@pytest.mark.parametrize("path", [_tool_call, _stream], ids=["tool call", "stream"])
@pytest.mark.parametrize("provider, effort, expected", CASES)
def test_the_effort_reaches_the_payload(monkeypatch, path, provider, effort, expected):
    seen = _capture(monkeypatch)
    path(provider, effort)
    assert {k: v for k, v in seen["payload"].items() if k in FIELDS} == expected


def test_a_plain_completion_sends_the_providers_default(monkeypatch):
    seen = _capture(monkeypatch)
    G._call_provider(_provider(model="qwen3.6:35b", reasoning_param="reasoning_effort", reasoning_effort="none"),
                     "hi", "", 64)
    assert seen["payload"]["reasoning_effort"] == "none"


def test_the_public_calls_take_a_reasoning_effort():
    for method in (gw.Gateway.complete_with_tools, gw.Gateway.stream_with_tools):
        assert "reasoning_effort" in inspect.signature(method).parameters, method.__name__


def test_the_settings_are_read_from_llm_providers_toml(tmp_path, monkeypatch):
    import socket

    real = socket.create_connection
    monkeypatch.setattr(socket, "create_connection",
                        lambda addr, *a, **k: (_ for _ in ()).throw(OSError("no local model in tests"))
                        if tuple(addr)[1] == 8080 else real(addr, *a, **k))
    (tmp_path / "llm-providers.toml").write_text(
        '[[gateway.providers]]\nname = "local-qwen"\nendpoint = "http://localhost:11434/v1"\nmodel = "qwen3.6"\n'
        'reasoning_param = "reasoning_effort"\nreasoning_effort = "none"\n', encoding="utf-8")
    p = next(p for p in gw.Gateway(config_dir=tmp_path).providers if p.name == "local-qwen")
    assert (p.reasoning_param, p.reasoning_effort) == ("reasoning_effort", "none")
