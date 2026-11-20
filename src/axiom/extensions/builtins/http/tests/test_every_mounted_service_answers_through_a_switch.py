# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Every service mounted on the composed app answers through a switch (ADR-182 D5).

A mounted service runs in the app's process and is switched with it, so the
app's own zero-gap proof (/healthz) shows the mechanism, not that each service
survives it. Here every mounted service is read during the switch, by one
cheap request each, and each must answer the same way before, during and
after: no refused request, no 5xx, nothing that only works on one copy.

This file is the `verified_by` of each service it reads. A service added to
SERVICES must answer here before it can stop saying "pending".
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from axiom.infra.change_intent import changes

#: One cheap read per mounted service: (extension, method, path, body).
SERVICES = [
    ("oauth", "GET", "/.well-known/openid-configuration", None),
    ("receipts", "GET", "/receipts/", None),
    ("webapp", "GET", "/api/v1/health", None),
    ("webgate", "GET", "/gate/login", None),
    ("program", "GET", "/program/status?scope=priorities", None),
    (
        "memory",
        "POST",
        "/v1/memory/recall",
        {"query": "switch probe", "consumer": {"principal": "@probe:test", "account": "probe"}, "k": 1},
    ),
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _call(port: int, method: str, path: str, body: dict | None, *, marker: str = "") -> int:
    """The status, or 0 when the body lacks ``marker`` (someone else answered)."""
    return _call_body(port, method, path, body, marker)


def _call_body(port: int, method: str, path: str, body: dict | None, marker: str) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Connection": "close"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        r = conn.getresponse()
        text = r.read().decode("utf-8", "replace")
        if marker and marker not in text:
            return 0
        return r.status
    finally:
        conn.close()


def _wait_ready(port: int, timeout_s: float = 300.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if _call(port, "GET", "/readyz", None) == 200:
                return
        except OSError:
            pass
        time.sleep(0.3)
    raise TimeoutError("supervised serve never became ready")


def _seed_program(state: Path) -> None:
    """program reads the node's own data file; give it the example program."""
    from axiom.extensions.builtins.program.tests.conftest import GENERIC_PROGRAM

    (state / "program").mkdir(parents=True)
    (state / "program" / "data.json").write_text(json.dumps(GENERIC_PROGRAM), encoding="utf-8")


def test_every_mounted_service_answers_through_a_switch(tmp_path, fresh_postgres):
    state = tmp_path / "state"
    state.mkdir()
    _seed_program(state)
    # The served app's memory mount opens the platform database at compose
    # time; the child gets its own throwaway one, never the developer's.
    env = {**os.environ, "AXI_STATE_DIR": str(state), "AXIOM_DB_URL": fresh_postgres}
    _switch_under_reads(SERVICES, env)
    shutil.rmtree(state, ignore_errors=True)


#: rag mounts only with a store configured, so it is proven against its own
#: throwaway database rather than in the shared run above.
RAG = [("rag", "GET", "/v1/models", None)]

#: A read that only this service answers. rag's routes are served at /v1 (the
#: composed app does not re-apply a MountSpec's prefix), alongside other /v1
#: routes, so its read is identified by the model it lists.
MARKERS = {"rag": "rag-model"}


def test_rag_answers_through_a_switch(tmp_path, fresh_postgres):
    env = {
        **os.environ,
        "AXI_STATE_DIR": str(tmp_path / "state"),
        "AXIOM_DB_URL": fresh_postgres,
        "AXIOM_RAG_DSN": fresh_postgres,
    }
    # rag declares deployment_profile = "server": it mounts on a server node.
    _switch_under_reads(RAG, env, profile="server")


def _switch_under_reads(services, env, *, profile: str | None = None) -> None:
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "axiom.extensions.builtins.http.cli", "--supervise",
         "--insecure", "--port", str(port), "--drain-s", "5",
         *(["--profile", profile] if profile else [])],
        env=env,
    )
    results: dict[str, dict] = {name: {"ok": 0, "bad": []} for name, *_ in services}
    switched_at: list[float] = []
    stop = threading.Event()
    baseline: dict[str, int] = {}

    def load() -> None:
        while not stop.is_set():
            for name, method, path, body in services:
                if stop.is_set():
                    return
                try:
                    status = _call(port, method, path, body, marker=MARKERS.get(name, ""))
                except Exception as exc:  # noqa: BLE001 - every client-visible failure counts
                    results[name]["bad"].append(f"{type(exc).__name__}: {exc}")
                    continue
                if status == baseline[name]:
                    results[name]["ok"] += 1
                else:
                    results[name]["bad"].append(f"HTTP {status} (idle: {baseline[name]})")
            time.sleep(0.05)

    try:
        _wait_ready(port)
        for name, method, path, body in services:
            baseline[name] = _call(port, method, path, body, marker=MARKERS.get(name, ""))
        # Each read must actually succeed while idle, or the proof proves nothing.
        assert all(s == 200 for s in baseline.values()), baseline
        t = threading.Thread(target=load, daemon=True)
        t.start()
        time.sleep(1.5)
        proc.send_signal(signal.SIGHUP)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline and not any(
            c.kind == "deploy" and c.outcome for c in changes()
        ):
            time.sleep(0.25)
        switched_at.append(time.monotonic())
        time.sleep(1.5)  # keep reading the new copy
        stop.set()
        t.join(timeout=60)
    finally:
        stop.set()
        proc.send_signal(signal.SIGTERM)
        code = proc.wait(timeout=60)

    assert [(c.kind, c.outcome) for c in changes() if c.kind == "deploy"] == [("deploy", "switched")]
    for name in results:
        assert results[name]["bad"] == [], (name, results[name]["bad"][:3])
        assert results[name]["ok"] >= 3, (name, results[name]["ok"])
    assert code == 0


def test_every_listed_service_names_this_file_as_its_proof():
    """The declaration and the proof agree: no service claims a proof that
    does not read it, and none read here is left saying pending."""
    import tomllib

    builtins = Path(__file__).resolve().parents[2]
    proof = "../http/tests/" + Path(__file__).name
    for name, *_ in SERVICES + RAG:
        manifest = tomllib.loads((builtins / name / "axiom-extension.toml").read_text())
        assert manifest["extension"]["availability"]["verified_by"] == proof, name


@pytest.mark.parametrize("name", [s[0] for s in SERVICES + RAG])
def test_the_proof_path_resolves_from_each_extension(name):
    builtins = Path(__file__).resolve().parents[2]
    assert (builtins / name / ("../http/tests/" + Path(__file__).name)).exists()
