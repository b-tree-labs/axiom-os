# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A completion keeps the model's reasoning apart from its answer, and says when there was no answer.

``CompletionResponse`` had no place for reasoning: the gateway read ``reasoning_content`` only to paste it in as the
answer when the answer was empty, with nothing marking it, and dropped it otherwise. Ollama names the field
``reasoning``, which neither the tool call nor the stream read, so a Qwen model's thinking was lost there. A surface
that shows reasoning to a person (folded away, as Open WebUI does) needs it as its own field, and a program needs to
know when the text it got is not an answer.
"""

from __future__ import annotations

import json

import pytest

from axiom.llm import gateway as gw

MSGS = [{"role": "user", "content": "hi"}]
G = gw.Gateway.__new__(gw.Gateway)
P = gw.LLMProvider(name="p", endpoint="http://localhost:11434/v1", model="m", api_key_env="")


class _Response:
    status_code = 200

    def __init__(self, message=None, finish="stop", deltas=()):
        self._message, self._finish, self._deltas = message or {}, finish, deltas

    def json(self):
        return {"choices": [{"message": self._message, "finish_reason": self._finish}]}

    def iter_lines(self, decode_unicode=True):
        for delta in self._deltas:
            yield "data: " + json.dumps({"choices": [{"delta": delta}]})
        yield "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": self._finish}]})  # as servers end
        yield "data: [DONE]"


def _serve(monkeypatch, response):
    monkeypatch.setattr(gw, "_post_with_rate_limit_retry", lambda *a, **k: response)


def _complete(monkeypatch, message, finish="stop"):
    _serve(monkeypatch, _Response(message=message, finish=finish))
    return G._call_provider_with_tools(P, MSGS, "", None, 64)


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning"])
def test_reasoning_comes_back_beside_the_answer(monkeypatch, field):
    r = _complete(monkeypatch, {"content": "The radius is 0.127 m.", field: "Look at the table first."})
    assert r.text == "The radius is 0.127 m."
    assert r.reasoning == "Look at the table first."
    assert r.reasoning_only is False


def test_inline_think_is_reasoning_too(monkeypatch):
    r = _complete(monkeypatch, {"content": "<think>Check the units.</think>0.127 m"})
    assert r.text == "0.127 m"
    assert r.reasoning == "Check the units."
    assert r.reasoning_only is False


@pytest.mark.parametrize("finish", ["stop", "length"])
def test_no_answer_is_flagged_and_the_text_is_unchanged(monkeypatch, finish):
    r = _complete(monkeypatch, {"content": "", "reasoning_content": "Still thinking about"}, finish=finish)
    assert r.reasoning_only is True
    assert r.reasoning == "Still thinking about"
    # What callers already relied on stays as it was.
    assert r.text.endswith("Still thinking about")
    assert r.text.startswith("[Response truncated") == (finish == "length")


def test_no_answer_on_ollama_is_flagged(monkeypatch):
    r = _complete(monkeypatch, {"content": "", "reasoning": "Still thinking about"})
    assert r.reasoning_only is True
    assert r.reasoning == "Still thinking about"
    assert r.text == ""  # unchanged: this field was never pasted in as the answer


def test_a_plain_answer_has_no_reasoning(monkeypatch):
    r = _complete(monkeypatch, {"content": "ok"})
    assert (r.text, r.reasoning, r.reasoning_only) == ("ok", "", False)


def test_the_stream_reads_ollamas_reasoning_field(monkeypatch):
    _serve(monkeypatch, _Response(deltas=[{"reasoning": "Check "}, {"reasoning": "units."}, {"content": "0.127 m"}]))
    chunks = list(G._stream_provider(P, MSGS, "", None, 64))
    assert "".join(c.text for c in chunks if c.type == "thinking_delta") == "Check units."
    assert "".join(c.text for c in chunks if c.type == "text") == "0.127 m"


def test_the_response_scan_covers_the_reasoning(monkeypatch):
    from axiom.llm.router import QueryRouter

    monkeypatch.setattr(QueryRouter, "_keyword_check", lambda self, text: ["probe-term"] if "probe-term" in text else [])
    r = gw.CompletionResponse(text="A clean answer.", reasoning="It mentions probe-term.", provider="p")
    scanned = gw._scan_response(r, "public", "p", "hash")
    assert scanned.text.startswith("[SECURITY WARNING")
    assert scanned.reasoning == "It mentions probe-term."  # kept, so the surface can still decide what to show


def test_the_response_scan_keeps_the_reasoning_fields(monkeypatch):
    from axiom.llm.router import QueryRouter

    monkeypatch.setattr(QueryRouter, "_keyword_check", lambda self, text: ["probe-term"])
    r = gw.CompletionResponse(text="probe-term", reasoning="trace", reasoning_only=True, provider="p")
    scanned = gw._scan_response(r, "public", "p", "hash")
    assert (scanned.reasoning, scanned.reasoning_only) == ("trace", True)


def _full_gateway(tmp_path, monkeypatch):
    import socket

    real = socket.create_connection
    monkeypatch.setattr(socket, "create_connection",
                        lambda addr, *a, **k: (_ for _ in ()).throw(OSError("no local model in tests"))
                        if tuple(addr)[1] == 8080 else real(addr, *a, **k))
    monkeypatch.setenv("TEST_REASONING_PROVIDER_KEY", "k")
    (tmp_path / "llm-providers.toml").write_text(
        '[[gateway.providers]]\nname = "thinker"\nendpoint = "https://llm.example/v1"\nmodel = "thinker-model"\n'
        'api_key_env = "TEST_REASONING_PROVIDER_KEY"\nrouting_tier = "public"\nuse_for = ["chat", "fallback"]\n',
        encoding="utf-8")
    return gw.Gateway(config_dir=tmp_path)


def test_a_reasoning_only_answer_is_still_scanned(tmp_path, monkeypatch):
    from axiom.llm.router import QueryRouter

    g = _full_gateway(tmp_path, monkeypatch)
    _serve(monkeypatch, _Response(message={"content": "", "reasoning": "It mentions probe-term."}))
    monkeypatch.setattr(QueryRouter, "_keyword_check", lambda self, text: ["probe-term"] if "probe-term" in text else [])
    r = g.complete_with_tools(messages=MSGS, max_tokens=64, routing_tier="public", prefer="thinker")
    assert r.reasoning_only is True
    assert r.text.startswith("[SECURITY WARNING")  # the scan ran although the answer text was empty


def test_a_tool_call_with_reasoning_is_not_a_missing_answer(monkeypatch):
    call = {"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
    r = _complete(monkeypatch, {"content": "", "reasoning": "I should look it up.", "tool_calls": [call]})
    assert r.reasoning == "I should look it up."
    assert r.reasoning_only is False  # it answered with a tool call
