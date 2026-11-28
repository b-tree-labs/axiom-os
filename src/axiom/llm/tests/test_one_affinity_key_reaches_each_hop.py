# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""One affinity key per session and entry reaches the hop that keeps the cache (#2211, RS-P9).

Without it OpenAI spreads requests that share a prefix across machines and OpenRouter pins a
provider only after a hit, so a conversation's cached history is thrown away between turns.
The key is a keyed hash: it names no person and carries no content.
"""

from __future__ import annotations

import random
import string

import pytest

from axiom.infra.trace import session_scope
from axiom.llm import affinity
from tests.routing_acceptance.test_p0 import _record_posts, _Sse, _usage_gateway

REPLY = {
    "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
}
USER = [{"role": "user", "content": "hello"}]


@pytest.fixture(autouse=True)
def _salt(tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    affinity._SALT = None


def _gateway(monkeypatch, endpoint, **entry):
    gw = _usage_gateway(monkeypatch, endpoint=endpoint, model="m")
    for name, value in entry.items():
        setattr(gw.providers[0], name, value)
    return gw


def _sent(monkeypatch, gw, session, stream=False):
    sent = _record_posts(monkeypatch, lambda _p: _Sse([], body=REPLY))
    with session_scope(session):
        if stream:
            list(gw._stream_openai(gw.providers[0], USER, "", None, 50))
        else:
            gw._call_openai_with_tools(gw.providers[0], USER, "", None, 50)
    return sent[-1]


def test_openrouter_gets_one_session_id_per_session(monkeypatch):
    gw = _gateway(monkeypatch, "https://openrouter.ai/api/v1")
    first, again = _sent(monkeypatch, gw, "s-1"), _sent(monkeypatch, gw, "s-1")
    other = _sent(monkeypatch, gw, "s-2")
    assert first["session_id"] == again["session_id"] != other["session_id"]


def test_openai_gets_a_prompt_cache_key_streamed_or_not(monkeypatch):
    gw = _gateway(monkeypatch, "https://api.openai.com/v1")
    assert (
        _sent(monkeypatch, gw, "s-1")["prompt_cache_key"]
        == _sent(monkeypatch, gw, "s-1", stream=True)["prompt_cache_key"]
    )


def test_an_entry_can_declare_its_field(monkeypatch):
    # A site proxy (LiteLLM on loopback) in front of a hosted model names the field itself.
    gw = _gateway(monkeypatch, "http://127.0.0.1:8700/v1", affinity_field="prompt_cache_key")
    assert "prompt_cache_key" in _sent(monkeypatch, gw, "s-1")


def test_an_undeclared_local_entry_gets_no_key(monkeypatch):
    payload = _sent(monkeypatch, _gateway(monkeypatch, "http://127.0.0.1:8700/v1"), "s-1")
    assert "prompt_cache_key" not in payload and "session_id" not in payload


def test_no_session_no_key(monkeypatch):
    payload = _sent(monkeypatch, _gateway(monkeypatch, "https://api.openai.com/v1"), "")
    assert "prompt_cache_key" not in payload


def test_the_key_differs_per_entry():
    a = affinity.affinity_key("s-1", "entry-a")
    assert a == affinity.affinity_key("s-1", "entry-a") != affinity.affinity_key("s-1", "entry-b")


def test_the_key_names_no_one_and_carries_no_content():
    rng = random.Random(7)
    for _ in range(200):
        # Long enough that finding it inside 32 hex characters would mean it was copied there.
        session = "".join(
            rng.choices(string.ascii_letters + string.digits + "@:.-", k=rng.randint(8, 40))
        )
        key = affinity.affinity_key(session, "uid-123")
        assert len(key) == 32 and set(key) <= set("0123456789abcdef")
        assert session not in key and "uid-123" not in key


def test_a_server_that_refuses_the_field_is_asked_again_without_it(monkeypatch):
    gw = _gateway(monkeypatch, "http://127.0.0.1:9000/v1", affinity_field="prompt_cache_key")
    calls = []

    class _Refused(Exception):
        response = type(
            "R", (), {"status_code": 400, "text": "Unrecognized request argument: prompt_cache_key"}
        )()

    def reply(payload):
        calls.append(dict(payload))
        if "prompt_cache_key" in payload:
            raise _Refused()
        return _Sse([], body=REPLY)

    _record_posts(monkeypatch, reply)
    with session_scope("s-1"):
        resp = gw._call_openai_with_tools(gw.providers[0], USER, "", None, 50)
    assert resp.text == "ok"
    assert "prompt_cache_key" in calls[0] and "prompt_cache_key" not in calls[-1]
