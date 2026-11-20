# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""OpenAI's reasoning models get ``max_completion_tokens``, everything else keeps ``max_tokens``.

The gateway sent ``max_tokens`` on every OpenAI-protocol request. OpenAI's gpt-5 and gpt-6 families and its o-series
reject that field with HTTP 400 ("Unsupported parameter: 'max_tokens' is not supported with this model. Use
'max_completion_tokens' instead."), so none of those models could be called through the gateway. OpenAI-compatible
servers such as Ollama and vLLM still expect ``max_tokens``, and Anthropic's API keeps its own ``max_tokens``.
"""

from __future__ import annotations

import pytest

from axiom.llm import gateway as gw


class _Response:
    status_code = 200

    def iter_lines(self, decode_unicode=True):
        yield 'data: {"choices":[{"delta":{"content":"ok"}}]}'
        yield "data: [DONE]"

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}], "content": [{"text": "ok"}]}


def _capture(monkeypatch):
    seen: dict = {}

    def post(requests_mod, url, payload, headers, timeout=60, **kwargs):
        seen["url"] = url
        seen["payload"] = payload
        return _Response()

    monkeypatch.setattr(gw, "_post_with_rate_limit_retry", post)
    return seen


def _provider(model, endpoint="https://api.openai.example/v1", **kw):
    return gw.LLMProvider(name="p", endpoint=endpoint, model=model, api_key_env="", **kw)


def _gw():
    return gw.Gateway.__new__(gw.Gateway)


REASONING = ["gpt-5", "gpt-5.4", "gpt-5.4-mini", "gpt-5-chat-latest", "gpt-6-luna", "gpt-6.1-sol", "o1", "o3-mini",
             "o4-mini", "@my-workspace/gpt-5.4"]
CLASSIC = ["gpt-4o", "gpt-4.1", "qwen3.6:35b-16k-t0", "gemma3:27b", "llama3.1:8b", "o3de-local", "gpt-50x-custom"]


@pytest.mark.parametrize("model", REASONING)
def test_reasoning_models_get_max_completion_tokens(model):
    assert gw._openai_token_limit(_provider(model), 64) == {"max_completion_tokens": 64}


@pytest.mark.parametrize("model", CLASSIC)
def test_other_models_keep_max_tokens(model):
    assert gw._openai_token_limit(_provider(model), 64) == {"max_tokens": 64}


def test_a_provider_can_name_the_field_explicitly():
    assert gw._openai_token_limit(_provider("my-gateway-alias", token_limit_param="max_completion_tokens"), 64) == {
        "max_completion_tokens": 64}
    assert gw._openai_token_limit(_provider("gpt-5.4", token_limit_param="max_tokens"), 64) == {"max_tokens": 64}


def test_the_floor_still_applies():
    assert gw._openai_token_limit(_provider("gpt-5.4", max_tokens_default=4096), 64) == {"max_completion_tokens": 4096}


@pytest.mark.parametrize("model, field", [("gpt-5.4", "max_completion_tokens"), ("qwen3.6:35b-16k-t0", "max_tokens")])
def test_every_openai_request_path_uses_the_right_field(monkeypatch, model, field):
    other = {"max_completion_tokens", "max_tokens"} - {field}
    msgs = [{"role": "user", "content": "hi"}]
    for call in (
        lambda g, p: g._call_provider(p, "hi", "", 64),
        lambda g, p: g._call_openai_with_tools(p, msgs, "", None, 64),
        lambda g, p: list(g._stream_openai(p, msgs, "", None, 64)),
    ):
        seen = _capture(monkeypatch)
        call(_gw(), _provider(model))
        assert seen["url"].endswith("/chat/completions")
        assert seen["payload"][field] == 64
        assert not other & set(seen["payload"])


def test_anthropic_requests_keep_their_own_max_tokens(monkeypatch):
    seen = _capture(monkeypatch)
    _gw()._call_provider(_provider("claude-model", endpoint="https://api.anthropic.com/v1"), "hi", "", 64)
    assert seen["url"].endswith("/messages")
    assert seen["payload"]["max_tokens"] == 64
    assert "max_completion_tokens" not in seen["payload"]


def test_the_setting_is_read_from_llm_providers_toml(tmp_path, monkeypatch):
    import socket

    real = socket.create_connection
    monkeypatch.setattr(socket, "create_connection",
                        lambda addr, *a, **k: (_ for _ in ()).throw(OSError("no local model in tests"))
                        if tuple(addr)[1] == 8080 else real(addr, *a, **k))
    (tmp_path / "llm-providers.toml").write_text(
        '[[gateway.providers]]\nname = "alias"\nendpoint = "https://llm.example/v1"\nmodel = "alias-model"\n'
        'token_limit_param = "max_completion_tokens"\n', encoding="utf-8")
    g = gw.Gateway(config_dir=tmp_path)
    p = next(p for p in g.providers if p.name == "alias")
    assert p.token_limit_param == "max_completion_tokens"
