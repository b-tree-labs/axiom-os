# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A one-time listener on this computer for a browser sign-in (RFC 8252 loopback).

The browser leg of a sign-in ends with a redirect to ``http://127.0.0.1:<port>/callback``
carrying a one-time code and the ``state`` this process chose. This listener is what
receives it. It is the CLI half that ``flow.py`` leaves to this layer.

- It binds **127.0.0.1 only**, on a port the OS picks, so nothing off this machine
  can reach it and no fixed port can be squatted on in advance.
- It accepts **one** answer: the first request to ``/callback`` whose ``state``
  matches. A request with any other state is answered and ignored, so a stray or
  hostile request to the port cannot end the sign-in or substitute a code.
- It stops after that answer, or at its deadline, whichever comes first.

Nothing it receives is printed. A code is useless without the PKCE verifier this
process holds, but it is still not something to put in a terminal.
"""

from __future__ import annotations

import hmac
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

CALLBACK_PATH = "/callback"


@dataclass(frozen=True)
class Callback:
    """What the browser brought back: a code, or the node's reason for refusing."""

    code: str = ""
    error: str = ""
    error_description: str = ""


_DONE_PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>{title}</title>
<style>:root{{color-scheme:light dark}}body{{font:16px/1.5 system-ui,sans-serif;max-width:32rem;
margin:4rem auto;padding:0 1rem}}</style></head><body><h1>{title}</h1><p>{body}</p></body></html>"""


class LoopbackListener:
    """Listen once on 127.0.0.1 for the browser's return. Use as a context manager."""

    def __init__(self, state: str, *, host: str = "127.0.0.1") -> None:
        self._state = state
        self._host = host
        self._result: Callback | None = None
        self._done = threading.Event()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def redirect_uri(self) -> str:
        assert self._server is not None, "start the listener first"
        port = self._server.server_address[1]
        return f"http://{self._host}:{port}{CALLBACK_PATH}"

    def start(self) -> LoopbackListener:
        listener = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:  # never echo a request line: it holds the code
                return

            def do_GET(self) -> None:  # noqa: N802 - http.server's name
                parts = urlsplit(self.path)
                if parts.path != CALLBACK_PATH:
                    self._reply(404, "Not here", "This address only answers a sign-in.")
                    return
                q = {k: v[0] for k, v in parse_qs(parts.query).items() if v}
                if listener._done.is_set() or not hmac.compare_digest(
                    q.get("state", ""), listener._state
                ):
                    self._reply(
                        400,
                        "Not this sign-in",
                        "This answer does not belong to the sign-in your command line started. "
                        "Nothing was changed.",
                    )
                    return
                listener._result = Callback(
                    code=q.get("code", ""),
                    error=q.get("error", ""),
                    error_description=q.get("error_description", ""),
                )
                if listener._result.code:
                    self._reply(200, "You are connected", "You can close this tab and go back to "
                                "your terminal.")
                else:
                    self._reply(200, "Sign-in not completed", "Go back to your terminal: it says "
                                "what happened and what to do.")
                listener._done.set()

            def _reply(self, status: int, title: str, body: str) -> None:
                page = _DONE_PAGE.format(title=title, body=body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)

        self._server = ThreadingHTTPServer((self._host, 0), _Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
        )
        self._thread.start()
        return self

    def wait(self, timeout_s: float) -> Callback | None:
        """The answer, or ``None`` if none came in time."""
        self._done.wait(timeout_s)
        return self._result

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def __enter__(self) -> LoopbackListener:
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.close()


__all__ = ["CALLBACK_PATH", "Callback", "LoopbackListener"]
