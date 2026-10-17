# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A served app is replaced under load with zero refused requests (ADR-182 D3).

The supervisor owns the listening socket. Each copy of the app is a child that
accepts on that same socket; a new copy says it is ready before the old one is
told to drain, and the kernel's queue of not-yet-accepted connections belongs to
the socket, not to either copy, so nothing queued is lost when the old copy stops
accepting. These tests drive real processes over real sockets: the claim is
about what a client sees, so a client is what measures it.
"""

from __future__ import annotations

import http.client
import os
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from axiom.infra.change_intent import changes
from axiom.infra.switch import Supervisor

CHILD = textwrap.dedent(
    """
    import os, time
    from fastapi import FastAPI
    from axiom.extensions.builtins.http.server import run_server

    if os.environ.get("CHILD_FAILS_TO_START"):
        raise SystemExit(3)
    # A slow start (a loaded low-power node), without loading the machine.
    time.sleep(float(os.environ.get("CHILD_START_DELAY_S", "0")))
    app = FastAPI()

    @app.get("/v")
    def v():
        return {"version": os.environ["CHILD_VERSION"]}

    @app.get("/slow")
    def slow():
        time.sleep(1.5)
        return {"version": os.environ["CHILD_VERSION"]}

    run_server(app)
    """
)


@pytest.fixture
def child_script(tmp_path: Path) -> Path:
    p = tmp_path / "child_app.py"
    p.write_text(CHILD)
    return p


def _argv(script: Path) -> list[str]:
    return [sys.executable, str(script)]


def _get(port: int, path: str = "/v", timeout: float = 10.0) -> tuple[int, str]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Connection": "close"})
        r = conn.getresponse()
        return r.status, r.read().decode()
    finally:
        conn.close()


class _Load:
    """Requests in a tight loop from several threads; records every outcome."""

    #: Each request is a new connection, so each leaves a client socket in
    #: TIME_WAIT. Unthrottled, back-to-back runs exhaust the client's ephemeral
    #: ports (EADDRNOTAVAIL) — the measuring client failing, not the server.
    #: ~100 requests/s across the threads stays far inside the port range.
    PAUSE_S = 0.02

    def __init__(self, port: int, threads: int = 2) -> None:
        self.port = port
        self.ok = 0
        self.failures: list[str] = []
        self.versions: set[str] = set()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._threads = [threading.Thread(target=self._run, daemon=True) for _ in range(threads)]

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                status, body = _get(self.port)
                with self._lock:
                    if status == 200:
                        self.ok += 1
                        self.versions.add(body)
                    else:
                        self.failures.append(f"HTTP {status}")
            except Exception as exc:  # noqa: BLE001 - every client-visible failure counts
                with self._lock:
                    self.failures.append(f"{type(exc).__name__}: {exc}")
            time.sleep(self.PAUSE_S)

    def __enter__(self) -> _Load:
        for t in self._threads:
            t.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=15)


def _sup(script: Path, version: str, **kw) -> Supervisor:
    return Supervisor(
        _argv(script),
        env={**os.environ, "CHILD_VERSION": version},
        drain_s=kw.pop("drain_s", 5.0),
        ready_timeout_s=kw.pop("ready_timeout_s", 30.0),
        **kw,
    )


def test_switching_under_load_refuses_nothing(child_script):
    sup = _sup(child_script, "1")
    sup.start()
    try:
        with _Load(sup.port) as load:
            time.sleep(1.0)
            for version in ("2", "3"):
                result = sup.switch(
                    env={**os.environ, "CHILD_VERSION": version}, subject=f"v{version}"
                )
                assert result.outcome == "switched", result.detail
                time.sleep(1.0)
        assert load.failures == [], load.failures[:5]
        assert load.ok > 50
        assert any('"3"' in v for v in load.versions)
        assert _get(sup.port)[1] == '{"version":"3"}'
    finally:
        sup.stop()


def test_a_request_in_flight_finishes_on_the_old_copy(child_script):
    sup = _sup(child_script, "old")
    sup.start()
    try:
        box: dict = {}
        t = threading.Thread(target=lambda: box.update(r=_get(sup.port, "/slow")))
        t.start()
        time.sleep(0.3)
        sup.switch(env={**os.environ, "CHILD_VERSION": "new"}, subject="vnew")
        t.join(timeout=15)
        assert box["r"] == (200, '{"version":"old"}')
    finally:
        sup.stop()


def test_a_copy_that_never_becomes_ready_is_never_switched_to(child_script):
    sup = _sup(child_script, "good", ready_timeout_s=5.0)
    sup.start()
    try:
        with _Load(sup.port) as load:
            result = sup.switch(
                env={**os.environ, "CHILD_VERSION": "bad", "CHILD_FAILS_TO_START": "1"},
                subject="vbad",
            )
            time.sleep(0.5)
        assert result.outcome == "rejected_before_switch"
        assert load.failures == [], load.failures[:5]
        assert _get(sup.port)[1] == '{"version":"good"}'
    finally:
        sup.stop()


def test_every_switch_is_recorded_as_a_deploy_before_it_acts(child_script):
    sup = _sup(child_script, "1", subject_prefix="probe-app")
    sup.start()
    try:
        sup.switch(env={**os.environ, "CHILD_VERSION": "2"}, subject="v2")
        sup.switch(
            env={**os.environ, "CHILD_VERSION": "x", "CHILD_FAILS_TO_START": "1"},
            subject="vx",
            ready_timeout_s=3.0,
        )
    finally:
        sup.stop()
    recorded = [(c.kind, c.subject, c.outcome) for c in changes()]
    assert recorded == [
        ("deploy", "probe-app v2", "switched"),
        ("deploy", "probe-app vx", "rejected_before_switch"),
    ]


def test_the_app_reports_not_ready_while_draining(child_script):
    """``/readyz`` is 200 while serving and 503 once a copy is draining, so a
    front door that balances across nodes stops sending it new work."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.http.server import install_readiness, mark_draining

    app = FastAPI()
    install_readiness(app)
    client = TestClient(app)
    assert client.get("/readyz").status_code == 200
    mark_draining(app)
    assert client.get("/readyz").status_code == 503


def test_a_connection_accepted_just_before_the_drain_is_still_served(child_script):
    """The race the accepted-connection grace exists for, made deterministic.

    A connection the draining copy has accepted but whose request it has not
    read yet is not "idle": its client has already committed to this copy.
    uvicorn on its own closes it the instant it stops accepting, and the client
    sees a reset. Under load that happened twice in two switches.
    """
    import signal
    import socket

    sup = _sup(child_script, "only")
    sup.start()
    try:
        sock = socket.create_connection(("127.0.0.1", sup.port), timeout=10)
        time.sleep(0.3)  # accepted by the running copy, nothing sent yet
        sup._current.send_signal(signal.SIGTERM)  # the drain half of a switch
        time.sleep(0.1)  # inside the grace
        sock.sendall(b"GET /v HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        reply = b""
        while chunk := sock.recv(4096):
            reply += chunk
        sock.close()
        assert reply.startswith(b"HTTP/1.1 200"), reply[:200]
        assert b'"only"' in reply
    finally:
        sup._current = None  # already draining; stop() would signal it again
        sup.stop()


def test_a_slow_start_within_the_timeout_is_switched_to(child_script):
    """A loaded low-power node can take minutes to start the app. Slow is not
    failed: the old copy keeps serving until the new one is ready."""
    sup = _sup(child_script, "1", ready_timeout_s=30.0)
    sup.start()
    try:
        with _Load(sup.port) as load:
            result = sup.switch(
                env={**os.environ, "CHILD_VERSION": "2", "CHILD_START_DELAY_S": "4"},
                subject="v2",
            )
            time.sleep(0.5)
        assert result.outcome == "switched", result.detail
        assert load.failures == [], load.failures[:5]
        assert _get(sup.port)[1] == '{"version":"2"}'
    finally:
        sup.stop()


def test_a_start_slower_than_the_timeout_is_abandoned_and_nothing_is_lost(child_script):
    sup = _sup(child_script, "1", ready_timeout_s=2.0)
    sup.start()
    try:
        with _Load(sup.port) as load:
            result = sup.switch(
                env={**os.environ, "CHILD_VERSION": "2", "CHILD_START_DELAY_S": "8"},
                subject="v2",
            )
        assert result.outcome == "rejected_before_switch"
        assert "not ready within 2s" in result.detail
        assert load.failures == [], load.failures[:5]
        assert _get(sup.port)[1] == '{"version":"1"}'
    finally:
        sup.stop()


def test_serve_waits_five_minutes_for_a_new_copy_by_default():
    """The served default must cover a slow node; 60s did not (measured: the
    composed app took longer than that to start under heavy load)."""
    from axiom.extensions.builtins.http.cli import get_parser

    assert get_parser().parse_args([]).ready_timeout_s == 300.0
