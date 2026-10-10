# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A streamed answer arrives in the characters it was written in.

A new user's first answer read::

    I'm part of the Axiom platform â an AI assistant ...

The model had written an em dash. A local model server streams Server-Sent
Events as ``Content-Type: text/event-stream`` with no charset, and
``requests`` decodes any ``text/*`` body without a charset as ISO-8859-1. So
the three UTF-8 bytes of "—" became "â" plus two invisible C1 control
characters — on every platform, before any terminal was involved.

The SSE format is UTF-8 by definition (WHATWG HTML §9.2), so the stream is
decoded as UTF-8 whatever the header omits. These tests run a real HTTP
server that answers exactly as the local server did.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from axiom.llm import gateway as gw

ANSWER = "I'm part of the platform — an assistant. Temperature 21 °C → fine."


def _openai_events(text: str) -> list[bytes]:
    events = []
    for piece, finish in ((text[:20], None), (text[20:], "stop")):
        events.append(
            b"data: "
            + json.dumps(
                {"choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": finish}]},
                ensure_ascii=False,
            ).encode("utf-8")
            + b"\n\n"
        )
    events.append(b"data: [DONE]\n\n")
    return events


def _anthropic_events(text: str) -> list[bytes]:
    lines = [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_stop"},
    ]
    return [
        b"data: " + json.dumps(e, ensure_ascii=False).encode("utf-8") + b"\n\n" for e in lines
    ]


@pytest.fixture
def sse_server():
    """A model server that streams UTF-8 and declares no charset."""
    state: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server's naming
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for event in state["events"]:
                self.wfile.write(event)
                self.wfile.flush()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{server.server_address[1]}"
    yield state
    server.shutdown()
    server.server_close()


def _provider(endpoint: str) -> gw.LLMProvider:
    return gw.LLMProvider(name="local", endpoint=endpoint, model="m", api_key_env="")


def _text(chunks) -> str:
    return "".join(c.text for c in chunks if c.type == "text")


def test_an_openai_compatible_stream_keeps_its_characters(sse_server):
    sse_server["events"] = _openai_events(ANSWER)
    gateway = gw.Gateway.__new__(gw.Gateway)
    chunks = list(
        gateway._stream_openai(
            _provider(sse_server["url"] + "/v1"), [{"role": "user", "content": "hi"}], "", None, 64
        )
    )
    assert _text(chunks) == ANSWER


def test_an_anthropic_stream_keeps_its_characters(sse_server):
    sse_server["events"] = _anthropic_events(ANSWER)
    gateway = gw.Gateway.__new__(gw.Gateway)
    chunks = list(
        gateway._stream_anthropic(
            _provider(sse_server["url"] + "/anthropic/v1"),
            [{"role": "user", "content": "hi"}],
            "",
            None,
            64,
        )
    )
    assert _text(chunks) == ANSWER
