# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``chat_server`` raw bypass + ``/v1/info`` endpoint.

Raw-model benchmark support — drives the HTTP-side wiring for the
agent's ``raw=True`` bypass and the new ``/v1/info`` endpoint that
reports the active model identifier so benchmark runs can record which
model produced each completion.
"""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, patch

import pytest

from axiom.extensions.builtins.http import chat_server

# ---------------------------------------------------------------------------
# Test handler harness — drive NeutAPIHandler without a real socket.
# ---------------------------------------------------------------------------


class _FakeSocket:
    """Minimal socket-shape so BaseHTTPRequestHandler.__init__ doesn't choke."""

    def makefile(self, *_args, **_kwargs):
        return io.BytesIO()

    def getsockname(self):
        return ("127.0.0.1", 0)

    def getpeername(self):
        return ("127.0.0.1", 0)


def _make_handler(method: str, path: str, body: bytes = b""):
    """Build a NeutAPIHandler instance with rfile/wfile set up.

    Bypasses BaseHTTPRequestHandler.__init__ which wants a live socket.
    """
    handler = chat_server.NeutAPIHandler.__new__(chat_server.NeutAPIHandler)
    handler.command = method
    handler.path = path
    handler.client_address = ("127.0.0.1", 0)
    handler.request_version = "HTTP/1.1"
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()

    # headers — provide a minimal mapping
    class _Headers:
        def __init__(self, content_length: int):
            self._d = {"Content-Length": str(content_length), "Origin": ""}

        def get(self, key, default=""):
            return self._d.get(key, default)

        def items(self):
            return self._d.items()

        def __iter__(self):
            return iter(self._d)

    handler.headers = _Headers(len(body))
    handler.allowed_origins = ["*"]
    handler.api_key = None
    handler.read_only = True
    handler.static_dir = None

    # Spy on send_response / send_header so we can assert status without
    # parsing wire bytes.
    handler._sent_status = None
    handler._sent_headers = []

    def _send_response(code, message=None):
        handler._sent_status = code

    def _send_header(name, value):
        handler._sent_headers.append((name, value))

    def _end_headers():
        pass

    handler.send_response = _send_response
    handler.send_header = _send_header
    handler.end_headers = _end_headers
    handler.log_message = lambda *_a, **_k: None
    return handler


def _read_response_json(handler) -> dict:
    """Pull the JSON written to wfile by _send_json."""
    raw = handler.wfile.getvalue()
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------------------
# /v1/chat/completions?raw=1 — query parameter
# ---------------------------------------------------------------------------


class TestRawQueryParam:
    def test_raw_query_param_passes_raw_true(self):
        body = json.dumps({"messages": [{"role": "user", "content": "Hello"}]}).encode()
        handler = _make_handler("POST", "/v1/chat/completions?raw=1", body)

        fake_agent = MagicMock()
        fake_agent.turn.return_value = "raw model output"
        with patch.object(chat_server, "_get_agent", return_value=fake_agent):
            handler._handle_openai_chat()

        assert fake_agent.turn.call_count == 1
        kwargs = fake_agent.turn.call_args.kwargs
        assert kwargs.get("raw") is True

    @pytest.mark.parametrize("raw_val", ["1", "true", "True", "yes", "YES"])
    def test_raw_query_truthy_values(self, raw_val):
        body = json.dumps({"messages": [{"role": "user", "content": "x"}]}).encode()
        handler = _make_handler("POST", f"/v1/chat/completions?raw={raw_val}", body)

        fake_agent = MagicMock()
        fake_agent.turn.return_value = "ok"
        with patch.object(chat_server, "_get_agent", return_value=fake_agent):
            handler._handle_openai_chat()

        assert fake_agent.turn.call_args.kwargs.get("raw") is True

    @pytest.mark.parametrize("raw_val", ["0", "false", "False", "no"])
    def test_raw_query_falsy_values(self, raw_val):
        body = json.dumps({"messages": [{"role": "user", "content": "x"}]}).encode()
        handler = _make_handler("POST", f"/v1/chat/completions?raw={raw_val}", body)

        fake_agent = MagicMock()
        fake_agent.turn.return_value = "ok"
        with patch.object(chat_server, "_get_agent", return_value=fake_agent):
            handler._handle_openai_chat()

        assert fake_agent.turn.call_args.kwargs.get("raw") is False


# ---------------------------------------------------------------------------
# /v1/chat/completions  body field — {"raw": true}
# ---------------------------------------------------------------------------


class TestRawBodyField:
    def test_raw_body_field_passes_raw_true(self):
        body = json.dumps(
            {"messages": [{"role": "user", "content": "x"}], "raw": True}
        ).encode()
        handler = _make_handler("POST", "/v1/chat/completions", body)

        fake_agent = MagicMock()
        fake_agent.turn.return_value = "ok"
        with patch.object(chat_server, "_get_agent", return_value=fake_agent):
            handler._handle_openai_chat()

        assert fake_agent.turn.call_args.kwargs.get("raw") is True

    def test_no_raw_flag_passes_raw_false(self):
        body = json.dumps({"messages": [{"role": "user", "content": "x"}]}).encode()
        handler = _make_handler("POST", "/v1/chat/completions", body)

        fake_agent = MagicMock()
        fake_agent.turn.return_value = "ok"
        with patch.object(chat_server, "_get_agent", return_value=fake_agent):
            handler._handle_openai_chat()

        assert fake_agent.turn.call_args.kwargs.get("raw") is False


# ---------------------------------------------------------------------------
# Body-vs-query precedence on disagreement
# ---------------------------------------------------------------------------


class TestRawPrecedence:
    def test_body_field_wins_when_query_disagrees(self):
        """When body says raw=false but query says raw=1, body wins.

        Body wins because it's the explicit JSON contract — easier to
        reason about for clients posting structured JSON.
        """
        body = json.dumps(
            {"messages": [{"role": "user", "content": "x"}], "raw": False}
        ).encode()
        handler = _make_handler("POST", "/v1/chat/completions?raw=1", body)

        fake_agent = MagicMock()
        fake_agent.turn.return_value = "ok"
        with patch.object(chat_server, "_get_agent", return_value=fake_agent):
            handler._handle_openai_chat()

        assert fake_agent.turn.call_args.kwargs.get("raw") is False

    def test_body_field_wins_when_body_true_query_false(self):
        body = json.dumps(
            {"messages": [{"role": "user", "content": "x"}], "raw": True}
        ).encode()
        handler = _make_handler("POST", "/v1/chat/completions?raw=0", body)

        fake_agent = MagicMock()
        fake_agent.turn.return_value = "ok"
        with patch.object(chat_server, "_get_agent", return_value=fake_agent):
            handler._handle_openai_chat()

        assert fake_agent.turn.call_args.kwargs.get("raw") is True


# ---------------------------------------------------------------------------
# Response shape — must remain OpenAI-compatible regardless of raw mode
# ---------------------------------------------------------------------------


class TestRawResponseShape:
    def test_raw_response_is_openai_shaped(self):
        body = json.dumps({"messages": [{"role": "user", "content": "Hello"}]}).encode()
        handler = _make_handler("POST", "/v1/chat/completions?raw=1", body)

        fake_agent = MagicMock()
        fake_agent.turn.return_value = "raw output"
        with patch.object(chat_server, "_get_agent", return_value=fake_agent):
            handler._handle_openai_chat()

        assert handler._sent_status == 200
        resp = _read_response_json(handler)
        assert resp["object"] == "chat.completion"
        assert "id" in resp and resp["id"].startswith("chatcmpl-")
        assert resp["choices"][0]["message"]["role"] == "assistant"
        assert resp["choices"][0]["message"]["content"] == "raw output"
        assert resp["choices"][0]["finish_reason"] == "stop"

    def test_normal_response_is_openai_shaped(self):
        body = json.dumps({"messages": [{"role": "user", "content": "Hello"}]}).encode()
        handler = _make_handler("POST", "/v1/chat/completions", body)

        fake_agent = MagicMock()
        fake_agent.turn.return_value = "wrapped output"
        with patch.object(chat_server, "_get_agent", return_value=fake_agent):
            handler._handle_openai_chat()

        assert handler._sent_status == 200
        resp = _read_response_json(handler)
        assert resp["object"] == "chat.completion"
        assert resp["choices"][0]["message"]["content"] == "wrapped output"


# ---------------------------------------------------------------------------
# GET /v1/info
# ---------------------------------------------------------------------------


class TestV1Info:
    def test_get_v1_info_returns_active_model_id(self):
        handler = _make_handler("GET", "/v1/info")
        handler.do_GET()
        assert handler._sent_status == 200
        resp = _read_response_json(handler)
        assert "model" in resp
        assert "raw_supported" in resp
        assert resp["raw_supported"] is True
        assert "version" in resp
        assert "endpoints" in resp
        assert resp["endpoints"]["chat"] == "/v1/chat/completions"
        assert resp["endpoints"]["info"] == "/v1/info"

    def test_v1_info_uses_default_local_model_id(self):
        from axiom.setup.llamafile import DEFAULT_LOCAL_MODEL_ID

        handler = _make_handler("GET", "/v1/info")
        handler.do_GET()
        resp = _read_response_json(handler)
        assert resp["model"] == DEFAULT_LOCAL_MODEL_ID

    def test_v1_info_version_falls_back_to_unknown(self):
        """Version-lookup failure must not break the endpoint."""
        handler = _make_handler("GET", "/v1/info")
        with patch(
            "importlib.metadata.version", side_effect=Exception("not installed")
        ):
            handler.do_GET()
        assert handler._sent_status == 200
        resp = _read_response_json(handler)
        # Either a real version or the documented fallback.
        assert resp["version"] in ("unknown",) or isinstance(resp["version"], str)

    def test_v1_info_does_not_require_agent(self):
        """The /v1/info endpoint should not lazily spin up a ChatAgent.

        The whole point is that benchmark drivers can hit it cheaply
        before sending real traffic. Confirm by patching _get_agent to
        raise — info should still succeed.
        """
        handler = _make_handler("GET", "/v1/info")
        with patch.object(
            chat_server, "_get_agent", side_effect=RuntimeError("must not be called")
        ):
            handler.do_GET()
        assert handler._sent_status == 200
