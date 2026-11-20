# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for the /compact tests.

The only thing faked is the network boundary: a real ``Gateway`` with real ``LLMProvider`` objects talks
HTTP to a local OpenAI-compatible server, so tier selection, placement and the request/response handling are
the shipped code.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

from axiom.infra.orchestrator.session import Session


class FakeLLM:
    """An OpenAI-compatible endpoint that records what it was sent."""

    def __init__(self, reply="SUMMARY: the user asked things; decisions were made.", status=200):
        self.reply = reply
        self.status = status
        self.requests: list[dict] = []
        outer = self

        class _H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                payload = json.dumps(
                    {
                        "choices": [
                            {"message": {"content": outer.reply}, "finish_reason": "stop"}
                        ],
                        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
                    }
                ).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):  # silence
                pass

        self._server = HTTPServer(("127.0.0.1", 0), _H)
        self.port = self._server.server_address[1]
        self.endpoint = f"http://127.0.0.1:{self.port}/v1"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self):
        self._server.shutdown()
        self._server.server_close()

    @property
    def prompts(self) -> str:
        return "\n".join(m["content"] for r in self.requests for m in r["messages"])


def provider(name, endpoint, *, tier="any", model="m", priority=10):
    from tests.test_gateway import _make_provider

    return _make_provider(
        name=name, endpoint=endpoint, model=model, api_key_env="", routing_tier=tier,
        priority=priority,
    )


def agent_with(providers, monkeypatch, session=None):
    from tests.test_gateway import _gateway_with_providers

    recorded: list = []
    return SimpleNamespace(
        gateway=_gateway_with_providers(providers, monkeypatch),
        session=session or Session(),
        _composition=None,
        _record_usage=recorded.append,
        usage_recorded=recorded,
    )


def conversation(turns=6, *, with_tools_in=(), words=60) -> Session:
    """A session of ``turns`` user/assistant exchanges; turns in ``with_tools_in`` call a tool."""
    s = Session(principal_id="@tester")
    for i in range(turns):
        s.add_message("user", f"question {i} " + "alpha " * words)
        if i in with_tools_in:
            s.add_message(
                "assistant", "", tool_calls=[{"name": "lookup", "id": f"t{i}", "input": {"q": i}}]
            )
            s.add_message(
                "tool", json.dumps({"value": 40 + i, "unit": "kg"}),
                tool_calls=[{"tool_call_id": f"t{i}", "name": "lookup"}],
            )
        s.add_message("assistant", f"answer {i} " + "beta " * words)
    return s
