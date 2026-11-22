# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A provider's ``request_timeout_s`` reaches every request it makes; unset, each path keeps its old default.

The timeouts were literals: 60 s for a plain completion, 120 s for the Anthropic tool call and for the gap between
streamed chunks, 180 s for the OpenAI tool call. A local vision model describing a full page answers after several
minutes, so the non-streaming call failed with "Read timed out" every time, and because a timeout is retried as a
transient error, each failure cost three times the limit before the caller heard about it.
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
        return {"choices": [{"message": {"content": "ok"}}], "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn"}


def _capture(monkeypatch):
    seen: dict = {}

    def post(requests_mod, url, payload, headers, timeout=60, **kwargs):
        seen["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(gw, "_post_with_rate_limit_retry", post)
    return seen


def _provider(endpoint, timeout_s=0):
    return gw.LLMProvider(name="p", endpoint=endpoint, model="m", api_key_env="", request_timeout_s=timeout_s)


OPENAI = "https://llm.example/v1"
ANTHROPIC = "https://api.anthropic.com/v1"
MSGS = [{"role": "user", "content": "hi"}]

PATHS = [  # (name, endpoint, call, old default)
    ("completion, OpenAI", OPENAI, lambda g, p: g._call_provider(p, "hi", "", 64), 60),
    ("completion, Anthropic", ANTHROPIC, lambda g, p: g._call_provider(p, "hi", "", 64), 60),
    ("tool call, OpenAI", OPENAI, lambda g, p: g._call_openai_with_tools(p, MSGS, "", None, 64), 180),
    ("tool call, Anthropic", ANTHROPIC, lambda g, p: g._call_anthropic_with_tools(p, MSGS, "", None, 64), 120),
    ("stream, OpenAI", OPENAI, lambda g, p: list(g._stream_openai(p, MSGS, "", None, 64)), 120),
    ("stream, Anthropic", ANTHROPIC, lambda g, p: list(g._stream_anthropic(p, MSGS, "", None, 64)), 120),
]


def _run(call, provider):
    try:
        call(gw.Gateway.__new__(gw.Gateway), provider)
    except Exception:  # noqa: BLE001 - only the request's timeout is under test, not parsing the fake reply
        pass


@pytest.mark.parametrize("name, endpoint, call, default", PATHS, ids=[p[0] for p in PATHS])
def test_the_provider_timeout_is_used(monkeypatch, name, endpoint, call, default):
    seen = _capture(monkeypatch)
    _run(call, _provider(endpoint, timeout_s=900))
    assert seen["timeout"] == 900


@pytest.mark.parametrize("name, endpoint, call, default", PATHS, ids=[p[0] for p in PATHS])
def test_unset_keeps_the_old_default(monkeypatch, name, endpoint, call, default):
    seen = _capture(monkeypatch)
    _run(call, _provider(endpoint))
    assert seen["timeout"] == default


def test_the_setting_is_read_from_llm_providers_toml(tmp_path, monkeypatch):
    import socket

    real = socket.create_connection
    monkeypatch.setattr(socket, "create_connection",
                        lambda addr, *a, **k: (_ for _ in ()).throw(OSError("no local model in tests"))
                        if tuple(addr)[1] == 8080 else real(addr, *a, **k))
    (tmp_path / "llm-providers.toml").write_text(
        '[[gateway.providers]]\nname = "slow-vision"\nendpoint = "http://localhost:11434/v1"\nmodel = "m"\n'
        'request_timeout_s = 900\n', encoding="utf-8")
    g = gw.Gateway(config_dir=tmp_path)
    assert next(p for p in g.providers if p.name == "slow-vision").request_timeout_s == 900
