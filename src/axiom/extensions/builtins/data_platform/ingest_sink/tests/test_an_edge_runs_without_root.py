# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""An edge runs on a host where its operator has no root.

The per-user unit and the no-linger fallback both start the edge from the
operator's own files, check disk first, and bind loopback only. The fallback
is exercised for real here: it starts this checkout's edge, answers health,
and stops it.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "ingest-edge"
SRC = Path(__file__).resolve().parents[6] / "src"


def test_the_user_unit_runs_from_the_operators_home_and_checks_disk_first():
    unit = (DEPLOY / "axiom-ingest-edge.user.service").read_text(encoding="utf-8")
    assert "User=" not in unit and "Group=" not in unit
    assert "EnvironmentFile=%h/.config/axiom-edge/edge.env" in unit
    assert "ExecStartPre=%h/axiom-edge/check-free-space.sh ${AXIOM_EDGE_DATA_DIR}" in unit
    assert "--host 127.0.0.1 --port ${AXIOM_EDGE_PORT}" in unit
    assert "WantedBy=default.target" in unit


def test_the_snippet_and_the_user_env_agree_on_the_port():
    env = (DEPLOY / "edge.user.env.example").read_text(encoding="utf-8")
    port = next(ln.split("=", 1)[1] for ln in env.splitlines() if ln.startswith("AXIOM_EDGE_PORT="))
    snippet = (DEPLOY / "nginx-axiom-edge.conf").read_text(encoding="utf-8")
    assert f"127.0.0.1:{port}" in snippet


def test_the_fallback_says_it_does_not_survive_a_reboot():
    text = (DEPLOY / "run-edge-without-linger.sh").read_text(encoding="utf-8")
    assert "DOES NOT SURVIVE A" in text and "REBOOT" in text


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(shutil.which("setsid") is None or shutil.which("df") is None,
                    reason="needs setsid and df (Linux hosts; the edge's target)")
def test_the_fallback_starts_the_edge_and_stops_it(tmp_path):
    home = tmp_path / "home"
    data = tmp_path / "data"
    (home / "axiom-edge" / "venv" / "bin").mkdir(parents=True)
    (home / ".config" / "axiom-edge").mkdir(parents=True)
    for d in ("state", "outbox", "bronze"):
        (data / d).mkdir(parents=True)
    shutil.copy(DEPLOY / "check-free-space.sh", home / "axiom-edge" / "check-free-space.sh")
    # This checkout's own CLI stands where the venv's `axi` would be.
    axi = home / "axiom-edge" / "venv" / "bin" / "axi"
    axi.write_text(f"#!/bin/sh\nPYTHONPATH={SRC} exec {sys.executable} -c "
                   "'import sys; from axiom.axiom_cli import main; sys.exit(main())' \"$@\"\n")
    axi.chmod(0o755)
    port = _free_port()
    env_file = home / ".config" / "axiom-edge" / "edge.env"
    env_file.write_text("\n".join([
        f"AXIOM_EDGE_PORT={port}", f"AXIOM_EDGE_DATA_DIR={data}", "AXIOM_EDGE_MIN_FREE_GIB=0",
        f"AXI_STATE_DIR={data}/state", f"AXIOM_INGEST_OUTBOX_DIR={data}/outbox",
        f"AXIOM_GATE_API_KEYS_FILE={data}/state/keys.json", "AXIOM_EDGE_DOWNSTREAM=@downstream:org",
    ]) + "\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AXIOM_", "AXI_"))}
    env["HOME"] = str(home)
    script = str(DEPLOY / "run-edge-without-linger.sh")

    start = subprocess.run(["sh", script, "start"], env=env, capture_output=True, text=True, timeout=60)
    try:
        assert start.returncode == 0, start.stderr
        assert "does not survive a reboot" in start.stdout
        for _ in range(150):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as r:  # noqa: S310
                    assert json.loads(r.read()).get("status") == "ok"
                    break
            except OSError:
                time.sleep(0.2)
        else:
            pytest.fail("edge never answered: " + (data / "edge.log").read_text())
        assert subprocess.run(["sh", script, "status"], env=env, capture_output=True, text=True).returncode == 0
    finally:
        stop = subprocess.run(["sh", script, "stop"], env=env, capture_output=True, text=True, timeout=30)
    assert "stopped" in stop.stdout
    assert subprocess.run(["sh", script, "status"], env=env, capture_output=True, text=True).returncode == 1
