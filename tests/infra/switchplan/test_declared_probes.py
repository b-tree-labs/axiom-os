# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""ADR-197 D2: every probe kind against a real thing, and the shared wait."""

from __future__ import annotations

import http.server
import socket
import sys
import threading

import pytest

from axiom.infra.switchplan.probes import (
    REGISTRY,
    EvalContext,
    Probe,
    ProbeRegistry,
    evaluate,
    fingerprint,
    wait_for,
)


@pytest.fixture
def server():
    state = {"status": 200}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(state["status"])
            self.end_headers()
            self.wfile.write(b'{"ready": true}')

        def log_message(self, *a):
            pass

    s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s.server_address[1], state
    s.shutdown()


def test_http_probe_checks_status_and_body(server):
    port, state = server
    url = f"http://127.0.0.1:{port}/readyz"
    assert evaluate(Probe("r", "http", {"url": url, "contains": "ready"})).ok
    assert not evaluate(Probe("r", "http", {"url": url, "contains": "nope"})).ok
    state["status"] = 503
    r = evaluate(Probe("r", "http", {"url": url}))
    assert not r.ok and "503" in r.detail


def test_tcp_probe(server):
    port, _ = server
    assert evaluate(Probe("p", "tcp", {"port": port})).ok
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert not evaluate(Probe("p", "tcp", {"port": free}, timeout_s=1)).ok


def test_command_probe_checks_the_exit_code():
    assert evaluate(Probe("c", "command", {"argv": [sys.executable, "-c", "raise SystemExit(0)"]})).ok
    r = evaluate(Probe("c", "command", {"argv": [sys.executable, "-c", "raise SystemExit(3)"]}))
    assert not r.ok and "exited 3" in r.detail
    assert not evaluate(Probe("c", "command", {"argv": ["/no/such/binary"]})).ok


def test_file_probe(tmp_path):
    f = tmp_path / "ptr"
    assert not evaluate(Probe("f", "file", {"path": str(f)})).ok
    f.write_text("v2")
    assert evaluate(Probe("f", "file", {"path": str(f), "contains": "v2"})).ok
    assert not evaluate(Probe("f", "file", {"path": str(f), "contains": "v1"})).ok


def test_a_malformed_probe_is_refused_at_declaration():
    with pytest.raises(ValueError):
        Probe("x", "ping")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Probe("x", "relation_read", {"name": "t"})
    with pytest.raises(ValueError):
        Probe("x", "callable")


def test_fingerprint_changes_with_the_declaration():
    a = [Probe("a", "tcp", {"port": 1})]
    assert fingerprint(a) == fingerprint([Probe("a", "tcp", {"port": 1})])
    assert fingerprint(a) != fingerprint([Probe("a", "tcp", {"port": 2})])


def test_the_registry_serves_one_definition_of_ready(server):
    port, state = server
    reg = ProbeRegistry()
    reg.declare("app", [Probe("up", "http", {"url": f"http://127.0.0.1:{port}/"})])
    assert reg.owners() == ["app"]
    assert all(r.ok for r in reg.wait_for("app", timeout_s=2))
    state["status"] = 500
    assert not all(r.ok for r in reg.wait_for("app", timeout_s=0.6, interval_s=0.1))
    with pytest.raises(KeyError):
        reg.wait_for("missing")
    assert REGISTRY is not reg


def test_wait_for_stops_early_when_the_thing_died():
    import time

    t0 = time.monotonic()
    out = wait_for([Probe("c", "command", {"argv": [sys.executable, "-c", "raise SystemExit(1)"]})],
                   timeout_s=30, interval_s=0.1, alive=lambda: False)
    assert not out[0].ok and time.monotonic() - t0 < 5


def test_relation_read_needs_a_cursor():
    r = evaluate(Probe("rr", "relation_read", {"name": "t", "must_scan": ["t"]}), EvalContext())
    assert not r.ok and "cursor" in r.detail
