# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`axi serve --supervise` is replaced on SIGHUP with no refused request.

This is the http extension's ADR-182 proof (its `verified_by`): the real CLI,
composing the real app, switched under load. A unit-level switch test proves
the mechanism; this proves the served node uses it.
"""

from __future__ import annotations

import http.client
import os
import signal
import socket
import subprocess
import sys
import threading
import time

from axiom.infra.change_intent import changes


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(port: int, path: str) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path, headers={"Connection": "close"})
        r = conn.getresponse()
        r.read()
        return r.status
    finally:
        conn.close()


def _wait_ready(port: int, timeout_s: float = 300.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if _get(port, "/readyz") == 200:
                return
        except OSError:
            pass
        time.sleep(0.2)
    raise TimeoutError("supervised serve never became ready")


def test_sighup_switches_the_served_app_with_no_refused_request(fresh_postgres):
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "axiom.extensions.builtins.http.cli",
            "--supervise",
            "--port",
            str(port),
            "--drain-s",
            "5",
        ],
        # The served app's memory mount opens the platform database at
        # compose time; the child gets a throwaway one, never the developer's.
        env={**os.environ, "AXIOM_DB_URL": fresh_postgres},
    )
    failures: list[str] = []
    ok = 0
    stop = threading.Event()

    def load() -> None:
        nonlocal ok
        while not stop.is_set():
            try:
                status = _get(port, "/healthz")
                if status == 200:
                    ok += 1
                else:
                    failures.append(f"HTTP {status}")
            except Exception as exc:  # noqa: BLE001 - every client-visible failure counts
                failures.append(f"{type(exc).__name__}: {exc}")
            time.sleep(0.02)

    try:
        _wait_ready(port)
        threads = [threading.Thread(target=load, daemon=True) for _ in range(2)]
        for t in threads:
            t.start()
        time.sleep(1.0)
        proc.send_signal(signal.SIGHUP)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline and not any(
            c.kind == "deploy" and c.outcome for c in changes()
        ):
            time.sleep(0.2)
        time.sleep(1.0)
        stop.set()
        for t in threads:
            t.join(timeout=15)
    finally:
        stop.set()
        proc.send_signal(signal.SIGTERM)
        code = proc.wait(timeout=30)

    assert failures == [], failures[:5]
    assert ok > 20
    recorded = [(c.kind, c.outcome) for c in changes() if c.kind == "deploy"]
    assert recorded == [("deploy", "switched")], recorded
    assert code == 0
