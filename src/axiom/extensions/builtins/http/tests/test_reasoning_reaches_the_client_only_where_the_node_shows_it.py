# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``/v1/chat/completions`` returns the turn's reasoning as ``reasoning_content`` where the deployment shows it.

Both implementations returned the answer text only, so a client that folds reasoning away (Open WebUI shows
``reasoning_content`` as a collapsed section) had nothing to fold, and a turn whose model stopped while reasoning
returned that reasoning as the answer, unmarked. Showing reasoning is a deployment decision, off unless enabled with
``AXIOM_SHOW_REASONING=1``, as ``raw`` is with ``AXIOM_ALLOW_RAW``: reasoning can quote retrieved passages and the
model's wrong turns. The request's ``reasoning_effort`` was dropped here too.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.http import chat_server
from axiom.extensions.builtins.http.tests.test_chat_server_raw import (
    _make_handler,
    _read_response_json,
)
from axiom.llm.reasoning import NO_ANSWER_NOTE, SHOW_REASONING_ENV

MESSAGES = [{"role": "user", "content": "What is the radius?"}]


def _agent(answer="0.127 m", reasoning="", reasoning_only=False, effort=""):
    agent = MagicMock()
    agent.turn.return_value = answer
    agent.scope = SimpleNamespace(last_reasoning=reasoning, last_reasoning_only=reasoning_only, reasoning_effort=effort)
    return agent


def _stdlib(payload, agent):
    handler = _make_handler("POST", "/v1/chat/completions", json.dumps(payload).encode())
    with patch.object(chat_server, "_get_agent", return_value=agent):
        handler._handle_openai_chat()
    return handler._sent_status, _read_response_json(handler)


def _fastapi(payload, agent):
    app = FastAPI()
    app.include_router(chat_server.build_chat_router(prefix=""))
    with patch.object(chat_server, "_get_agent", return_value=agent):
        resp = TestClient(app).post("/v1/chat/completions", json=payload)
    return resp.status_code, resp.json()


CALLS = pytest.mark.parametrize("call", [_stdlib, _fastapi], ids=["stdlib", "fastapi"])


def _message(call, agent, **payload):
    status, body = call({"messages": MESSAGES, **payload}, agent)
    assert status == 200, body
    return body["choices"][0]["message"]


@CALLS
def test_off_by_default_the_answer_is_unchanged(call, monkeypatch):
    monkeypatch.delenv(SHOW_REASONING_ENV, raising=False)
    message = _message(call, _agent(reasoning="Check the table first."))
    assert message == {"role": "assistant", "content": "0.127 m"}


@CALLS
def test_on_the_reasoning_comes_beside_the_answer(call, monkeypatch):
    monkeypatch.setenv(SHOW_REASONING_ENV, "1")
    message = _message(call, _agent(reasoning="Check the table first."))
    assert message["content"] == "0.127 m"
    assert message["reasoning_content"] == "Check the table first."


@CALLS
def test_on_a_turn_with_no_answer_says_so_instead_of_repeating_the_reasoning(call, monkeypatch):
    monkeypatch.setenv(SHOW_REASONING_ENV, "true")
    message = _message(call, _agent(answer="Still thinking about", reasoning="Still thinking about", reasoning_only=True))
    assert message["content"] == NO_ANSWER_NOTE
    assert message["reasoning_content"] == "Still thinking about"


@CALLS
def test_on_without_reasoning_adds_nothing(call, monkeypatch):
    monkeypatch.setenv(SHOW_REASONING_ENV, "1")
    assert "reasoning_content" not in _message(call, _agent())


@CALLS
def test_an_unrecognised_value_is_off(call, monkeypatch):
    monkeypatch.setenv(SHOW_REASONING_ENV, "maybe")
    assert "reasoning_content" not in _message(call, _agent(reasoning="Check the table first."))


@CALLS
def test_the_requests_reasoning_effort_reaches_the_turn(call):
    agent = _agent()
    _message(call, agent, reasoning_effort="Low")
    assert agent.scope.reasoning_effort == "low"


@CALLS
def test_a_request_without_an_effort_does_not_keep_the_last_ones(call):
    agent = _agent(effort="high")  # a cached agent, last used with "high"
    _message(call, agent)
    assert agent.scope.reasoning_effort == ""


@CALLS
def test_an_unknown_effort_is_a_400(call):
    agent = _agent()
    status, body = call({"messages": MESSAGES, "reasoning_effort": "maximum"}, agent)
    assert status == 400 and "reasoning_effort" in body["error"]["message"]
    agent.turn.assert_not_called()
