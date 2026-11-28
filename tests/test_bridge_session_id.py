# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""SID-A4 (#2226): the bridge gives a harness conversation one session id.

A harness (Claude Code, Codex, an OpenAI SDK client) talks to the gateway
through ``axiom.llm.anthropic_ingress``. Each request names its conversation in
one of three ways, tried in order: a stated session header, ``metadata.user_id``,
or neither, when the stable prefix (system prompt, tools, first message) is
what stays the same across turns. Whichever applies, two turns of one
conversation reach the gateway with the same id, and the id is never the
principal's.
"""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from axiom.infra import trace

PRINCIPAL = "@alice:example"


class _Gateway:
    def __init__(self, seen: list[str]) -> None:
        self.providers = []
        self._seen = seen

    def complete_with_tools(self, *_a, **_kw):
        self._seen.append(trace.current_session())
        return SimpleNamespace(text="ok", tool_calls=[], stop_reason="end_turn",
                               usage={}, success=True)


class _Router:
    def classify(self, *_a, **_kw):
        return SimpleNamespace(tier=SimpleNamespace(value="any"))


@pytest.fixture
def bridge(monkeypatch):
    """A bridge handler wired to a gateway that records the session it ran under."""
    from axiom.llm import anthropic_ingress, gateway, router

    seen: list[str] = []
    monkeypatch.setenv("AXIOM_BRIDGE_WEB_TOOLS", "0")
    monkeypatch.setattr(gateway, "Gateway", lambda *a, **k: _Gateway(seen))
    monkeypatch.setattr(router, "QueryRouter", lambda *a, **k: _Router())
    handler_cls = anthropic_ingress._build_handler()

    def post(body: dict, headers: dict | None = None) -> None:
        raw = json.dumps(body).encode()
        h = handler_cls.__new__(handler_cls)
        h.path = "/v1/messages"
        h.rfile = io.BytesIO(raw)
        h.wfile = io.BytesIO()
        h.headers = {"Content-Length": str(len(raw)), **(headers or {})}
        h.request_version = "HTTP/1.1"
        h.send_response = lambda *a, **k: None
        h.send_header = lambda *a, **k: None
        h.end_headers = lambda *a, **k: None
        h.do_POST()

    return SimpleNamespace(post=post, seen=seen)


def _turns(system: str, first: str) -> tuple[dict, dict]:
    """Two requests of one conversation: the second repeats the first and adds a turn."""
    one = {"model": "m", "max_tokens": 64, "system": system,
           "messages": [{"role": "user", "content": first}]}
    two = {**one, "messages": one["messages"] + [
        {"role": "assistant", "content": "ok"}, {"role": "user", "content": "and then?"}]}
    return one, two


def _assert_one_id_per_conversation(seen: list[str]) -> None:
    a1, a2, b1 = seen
    assert a1 == a2, "two turns of one conversation must share an id"
    assert a1 != b1, "two conversations must not share an id"
    for sid in seen:
        assert sid not in ("", "anonymous")
        assert sid != PRINCIPAL and PRINCIPAL not in sid


def test_sid_a4_session_header(bridge):
    one, two = _turns("sys", "hi")
    bridge.post(one, {"X-Claude-Code-Session-Id": "conv-1"})
    bridge.post(two, {"X-Claude-Code-Session-Id": "conv-1"})
    bridge.post(one, {"X-Claude-Code-Session-Id": "conv-2"})
    _assert_one_id_per_conversation(bridge.seen)


def test_sid_a4_metadata_user_id(bridge):
    one, two = _turns("sys", "hi")
    a = {"metadata": {"user_id": f"{PRINCIPAL}_session_1"}}
    b = {"metadata": {"user_id": f"{PRINCIPAL}_session_2"}}
    bridge.post({**one, **a})
    bridge.post({**two, **a})
    bridge.post({**one, **b})
    _assert_one_id_per_conversation(bridge.seen)


def test_sid_a4_neither_uses_the_stable_prefix(bridge):
    one, two = _turns("you are a coding agent", "fix the build")
    other, _ = _turns("you are a coding agent", "write the docs")
    bridge.post(one)
    bridge.post(two)
    bridge.post(other)
    _assert_one_id_per_conversation(bridge.seen)


def test_sid_a4_the_id_does_not_outlive_the_request(bridge):
    one, _ = _turns("sys", "hi")
    bridge.post(one, {"X-Claude-Code-Session-Id": "conv-1"})
    assert trace.current_session() == "anonymous"
