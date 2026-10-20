# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A real HTTP server shaped like a gated single-page app (no mocks)."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

SESSION = "sid=good"
SHELL = b'<html><head><script src="/app.js"></script></head><body>app</body></html>'
LOGIN = b"<html><body>sign in</body></html>"


def make_handler(
    *,
    shell_cache_control: str | None,
    assets_become_login: bool = False,
    asset_is_html: bool = False,
):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, code, body, ctype, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            authed = SESSION in (self.headers.get("Cookie") or "")
            if self.path.startswith("/gate/login"):
                return self._send(200, LOGIN, "text/html", {"Set-Cookie": "probe=secret-value"})
            if self.path == "/api/config":
                if not authed:
                    return self._send(401, b'{"detail":"expired"}', "application/json")
                return self._send(200, b"{}", "application/json")
            if self.path == "/app.js":
                if not authed:
                    if assets_become_login:
                        return self._send(200, LOGIN, "text/html")
                    return self._send(
                        302, b"", "text/html", {"Location": "/gate/login?next=/app.js"}
                    )
                if asset_is_html:
                    return self._send(200, LOGIN, "text/html")
                return self._send(200, b"console.log(1)", "application/javascript")
            if not authed:
                return self._send(
                    302, b"", "text/html", {"Location": "/gate/login?next=" + self.path}
                )
            extra = {"Last-Modified": "Thu, 27 Aug 2026 00:37:04 GMT", "ETag": '"abc"'}
            if shell_cache_control:
                extra["Cache-Control"] = shell_cache_control
            return self._send(200, SHELL, "text/html; charset=utf-8", extra)

    return H


@pytest.fixture
def serve():
    servers: list[HTTPServer] = []

    def start(**kw) -> str:
        srv = HTTPServer(("127.0.0.1", 0), make_handler(**kw))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


JOURNEY = {
    "journey": {
        "name": "gated-ui",
        "base_url": "http://unused",
        "session": {"cookie_env": "SYNTH_COOKIE"},
    },
    "step": [
        {
            "name": "signed-out visitor is sent to sign in",
            "path": "/",
            "headers": {"Accept": "text/html"},
            "expect": {"status_in": [302, 303, 307], "location_matches": "/gate/login"},
        },
        {
            "name": "signed-out API call answers 401 JSON",
            "path": "/api/config",
            "expect": {"status": 401, "content_type": "application/json"},
        },
        {
            "name": "signed-in page must be revalidated",
            "path": "/",
            "auth": "session",
            "expect": {
                "status": 200,
                "content_type": "text/html",
                "cache_policy": "revalidate",
                "assets_not_html": True,
            },
        },
    ],
}
