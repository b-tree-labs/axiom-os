# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A sender's key goes over https, and goes only where it was sent.

A sender configured with ``http://`` for a public intake sent its bearer key in
the clear, then (because the public host answers port 80 with a redirect to
https and urllib follows it) again to wherever the redirect pointed, with the
``Authorization`` header kept. Nothing in the sender objected. On a public edge
that turns one mistyped scheme into a leaked credential.

What has to hold:

- ``https`` is always allowed; ``http`` is allowed to this machine and to
  private networks (a node talking to its own edge on a compose network), and
  refused to a host that resolves to a public address;
- the refusal is a transport failure the sender already treats as "hold the
  data and say why", with words that name the fix;
- a redirect is never followed, so a key cannot be handed to the host a
  redirect names.
"""

from __future__ import annotations

import http.server
import threading

import pytest

from axiom.extensions.builtins.data_platform.daq.transmitter import UrllibTransport
from axiom.extensions.builtins.data_platform.forward.forwarder import IntakeTarget
from axiom.infra.credential_transport import PlaintextRefused, check_url


@pytest.mark.parametrize("url", [
    "https://nuclear-twins.example.org",
    "http://127.0.0.1:8787",
    "http://localhost:8787",
    "http://10.1.2.3",
    "http://192.168.1.5:8787",
    "http://172.20.0.2:8787",
    "http://[::1]:8787",
])
def test_these_are_allowed(url):
    check_url(url)


@pytest.mark.parametrize("url", [
    "http://8.8.8.8",
    "http://1.1.1.1:8787/ingest/rows",
    "http://[2606:4700:4700::1111]",
    "ftp://127.0.0.1",
    "file:///etc/passwd",
    "127.0.0.1:8787",
])
def test_these_are_refused(url):
    with pytest.raises(PlaintextRefused):
        check_url(url)


def test_the_refusal_names_the_fix():
    with pytest.raises(PlaintextRefused) as exc:
        check_url("http://8.8.8.8")
    assert "https://" in str(exc.value) and "8.8.8.8" in str(exc.value)
    assert isinstance(exc.value, OSError)  # senders hold the data on an OSError


def test_the_transport_refuses_before_any_connection():
    with pytest.raises(PlaintextRefused):
        UrllibTransport(timeout_s=0.2).post("http://8.8.8.8/ingest/rows", b"{}", {"Authorization": "Bearer k"})


def test_the_forwarders_intake_refuses_too():
    target = IntakeTarget("http://8.8.8.8", token="k", probe_source="s")
    with pytest.raises(PlaintextRefused):
        target._post(b"{}")


class _Recorder(http.server.BaseHTTPRequestHandler):
    seen: list[dict] = []
    redirect_to: str | None = None

    def do_POST(self):  # noqa: N802
        type(self).seen.append(dict(self.headers))
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if type(self).redirect_to:
            self.send_response(301)
            self.send_header("Location", type(self).redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


def _serve():
    handler = type("H", (_Recorder,), {"seen": [], "redirect_to": None})
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, handler


@pytest.fixture
def two_servers():
    first, h1 = _serve()
    second, h2 = _serve()
    h1.redirect_to = f"http://127.0.0.1:{second.server_port}/stolen"
    yield first, h1, second, h2
    first.shutdown()
    second.shutdown()


def test_a_redirect_is_not_followed_and_the_key_stays_home(two_servers):
    first, h1, _second, h2 = two_servers
    out = UrllibTransport().post(
        f"http://127.0.0.1:{first.server_port}/ingest/rows", b"{}",
        {"Authorization": "Bearer secret-key", "Content-Type": "application/json"})
    assert out[0] == 301
    assert len(h1.seen) == 1
    assert h2.seen == []


def test_the_forwarder_does_not_follow_a_redirect_either(two_servers):
    first, h1, _second, h2 = two_servers
    target = IntakeTarget(f"http://127.0.0.1:{first.server_port}", token="secret-key", probe_source="s")
    status, _answer, _headers = target._post(b"{}")
    assert status == 301
    assert h2.seen == []


def test_a_plain_loopback_push_still_works(two_servers):
    _first, _h1, second, h2 = two_servers
    out = UrllibTransport().post(f"http://127.0.0.1:{second.server_port}/ingest/rows", b"{}", {})
    assert out[0] == 200 and len(h2.seen) == 1
