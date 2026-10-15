# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""An intake that drops a connection is a failed probe, never a crashed forwarder.

The probe's second half (an empty batch, proving the key) was outside the
probe's own error handling. Found 2026-10-08 in a scaled reconnect flood: the
host ran short of ephemeral ports (Errno 49), that one request raised, and the
forwarder's loop died mid-drain with 30 batches still to send. Under
``data.forward --watch`` the same error ends the watch.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from axiom.extensions.builtins.data_platform.forward import IntakeTarget


class _HealthyButDropsPosts(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_POST(self):  # noqa: N802
        self.close_connection = True  # no answer at all: the client sees the connection drop

    def log_message(self, *a):
        pass


def test_a_dropped_probe_request_is_a_failed_probe():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HealthyButDropsPosts)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        target = IntakeTarget(f"http://127.0.0.1:{server.server_port}", token="t", probe_source="s")
        p = target.probe()
        assert p.ok is False
        assert "unreachable" in p.reason or "answered" in p.reason
        ok, why = target.send({"seq": 1}, b"{}")
        assert ok is False and "unreachable" in why
    finally:
        server.shutdown()
