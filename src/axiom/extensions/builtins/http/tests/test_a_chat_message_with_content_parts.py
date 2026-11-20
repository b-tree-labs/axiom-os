# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``/v1/chat/completions`` accepts OpenAI content parts: text parts are joined, other parts get a 400.

Both implementations took the last user message's ``content`` as-is and passed it to ``agent.turn``. A list of parts
(the shape OpenAI SDK clients send, and the only shape that can carry an image) reached a regex in the per-prompt
override parser and failed as an HTTP 500 with a TypeError.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.http import chat_server
from axiom.extensions.builtins.http.chat_server import user_message_text
from axiom.extensions.builtins.http.tests.test_chat_server_raw import (
    _make_handler,
    _read_response_json,
)

TEXT_PARTS = [{"type": "text", "text": "What is the core radius?"}, {"type": "text", "text": "Cite the page."}]
IMAGE_PARTS = [{"type": "text", "text": "Describe this."},
               {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]


def test_the_helper():
    assert user_message_text("plain") == ("plain", None)
    assert user_message_text(TEXT_PARTS) == ("What is the core radius?\nCite the page.", None)
    text, error = user_message_text(IMAGE_PARTS)
    assert text == "" and "image_url" in error
    assert user_message_text(42)[1] is not None


def _stdlib(content):
    body = json.dumps({"messages": [{"role": "user", "content": content}]}).encode()
    handler = _make_handler("POST", "/v1/chat/completions", body)
    agent = MagicMock()
    agent.turn.return_value = "ok"
    with patch.object(chat_server, "_get_agent", return_value=agent):
        handler._handle_openai_chat()
    return handler._sent_status, _read_response_json(handler), agent


def _fastapi(content):
    app = FastAPI()
    app.include_router(chat_server.build_chat_router(prefix=""))
    agent = MagicMock()
    agent.turn.return_value = "ok"
    with patch.object(chat_server, "_get_agent", return_value=agent):
        resp = TestClient(app).post("/v1/chat/completions", json={"messages": [{"role": "user", "content": content}]})
    return resp.status_code, resp.json(), agent


@pytest.mark.parametrize("call", [_stdlib, _fastapi], ids=["stdlib", "fastapi"])
def test_text_parts_reach_the_agent_as_text(call):
    status, _body, agent = call(TEXT_PARTS)
    assert status == 200
    assert agent.turn.call_args.args[0] == "What is the core radius?\nCite the page."


@pytest.mark.parametrize("call", [_stdlib, _fastapi], ids=["stdlib", "fastapi"])
def test_an_image_part_is_a_clear_400(call):
    status, body, agent = call(IMAGE_PARTS)
    assert status == 400
    assert "image_url" in body["error"]["message"]
    agent.turn.assert_not_called()


@pytest.mark.parametrize("call", [_stdlib, _fastapi], ids=["stdlib", "fastapi"])
def test_a_plain_string_is_unchanged(call):
    status, _body, agent = call("Hello")
    assert status == 200 and agent.turn.call_args.args[0] == "Hello"
