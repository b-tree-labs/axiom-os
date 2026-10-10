# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""An MCP client keeps working across a switch with no reconnect (ADR-182 D3).

The HTTP transport holds no per-client state: every JSON-RPC POST stands
alone. So a client that initialized against the old copy calls tools on the
new copy without initializing again, and tool calls in flight across the
switch all succeed. This is the mcp extension's ADR-182 proof.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

from axiom.infra.change_intent import changes


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _rpc(port: int, method: str, rid: int) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/mcp",
        data=json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": {}}).encode(),
        headers={"content-type": "application/json", "connection": "close"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


def _wait_ready(port: int) -> None:
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/readyz", timeout=2) as r:
                if r.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.3)
    raise TimeoutError("server never became ready")


def test_an_mcp_client_keeps_working_across_a_switch(fresh_postgres):
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "axiom.extensions.builtins.http.cli",
            "--supervise",
            "--insecure",
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
    calls = 0
    stop = threading.Event()

    def load() -> None:
        nonlocal calls
        rid = 1000
        while not stop.is_set():
            rid += 1
            try:
                out = _rpc(port, "tools/list", rid)
                if "result" in out and out["result"]["tools"]:
                    calls += 1
                else:
                    failures.append(str(out)[:200])
            except Exception as exc:  # noqa: BLE001 - every client-visible failure counts
                failures.append(f"{type(exc).__name__}: {exc}")
            time.sleep(0.05)

    try:
        _wait_ready(port)
        assert _rpc(port, "initialize", 1)["result"]["serverInfo"]["name"] == "axiom-root"
        t = threading.Thread(target=load, daemon=True)
        t.start()
        time.sleep(1.0)
        proc.send_signal(signal.SIGHUP)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline and not any(
            c.kind == "deploy" and c.outcome for c in changes()
        ):
            time.sleep(0.25)
        # No second initialize: the same client simply carries on.
        assert _rpc(port, "tools/list", 2)["result"]["tools"]
        time.sleep(1.0)
        stop.set()
        t.join(timeout=30)
    finally:
        stop.set()
        proc.send_signal(signal.SIGTERM)
        code = proc.wait(timeout=60)

    assert failures == [], failures[:5]
    assert calls > 5
    assert [(c.kind, c.outcome) for c in changes() if c.kind == "deploy"] == [
        ("deploy", "switched")
    ]
    assert code == 0
