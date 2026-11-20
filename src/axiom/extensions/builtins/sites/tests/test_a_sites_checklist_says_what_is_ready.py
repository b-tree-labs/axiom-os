# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi sites checklist <site>`` is the definition of "ready" (ADR-178 D4).

Every check prints as ``ok``, ``waiting``, ``failing`` (with the fix) or
``not applicable``. A check this node cannot verify yet is ``failing`` with
what is missing named: a site is never shown ready on evidence nobody has.
The health checks come from the site's heartbeats (W8); the rest wait on the
site registry (ADR-178 D1) and say so.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.data_platform.ingest_sink.heartbeat import HeartbeatStore
from axiom.extensions.builtins.sites import checklist

NOW = time.time()


def _iso(seconds_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds_ago)).isoformat()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_HEARTBEAT_DIR", str(tmp_path / "hb"))
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "st"))
    monkeypatch.delenv("AXIOM_SERVED_SITES", raising=False)
    return HeartbeatStore(tmp_path / "hb")


def _by_check(rows):
    return {r["check"]: r for r in rows}


def test_a_reporting_site_passes_the_checks_its_heartbeat_answers(store):
    store.record(
        "site-a",
        {
            "node": "daq-1",
            "version": "1.17.1",
            "last_reading_at": _iso(20),
            "lanes": {"store_and_forward": {"current": "intake"}},
            "disk_used": 0.4,
        },
        received_at=NOW - 20,
    )
    rows = checklist.run("site-a", now=NOW, min_version="1.17.0")
    c = _by_check(rows)
    assert c["Heartbeat recent"]["state"] == "ok"
    assert (
        c["Forwarder transport"]["state"] == "ok" and "intake" in c["Forwarder transport"]["detail"]
    )
    assert c["Node version in the compatibility window"]["state"] == "ok"
    assert c["Archive disk"]["state"] == "ok"
    assert set(r["state"] for r in rows) <= {"ok", "waiting", "failing", "not applicable"}


def test_a_silent_site_fails_the_heartbeat_check_with_the_fix(store):
    store.record("site-a", {"node": "daq-1"}, received_at=NOW - 3 * 3600)
    c = _by_check(checklist.run("site-a", now=NOW))
    assert c["Heartbeat recent"]["state"] == "failing" and c["Heartbeat recent"]["fix"]


def test_a_site_nobody_has_heard_from_is_waiting_not_failing(store):
    c = _by_check(checklist.run("site-z", now=NOW))
    assert c["Heartbeat recent"]["state"] == "waiting"
    assert "not yet installed" in c["Heartbeat recent"]["detail"]


def test_a_check_nobody_can_verify_yet_fails_and_names_what_is_missing(store):
    store.record("site-a", {"node": "daq-1"}, received_at=NOW - 20)
    c = _by_check(checklist.run("site-a", now=NOW))
    for name in (
        "Record complete",
        "Sending key issued and redeemed",
        "People can sign in and see only their site",
    ):
        assert c[name]["state"] == "failing", name
        assert "cannot verify" in c[name]["detail"] and "ADR-178" in c[name]["fix"], name
    # Every ADR-178 D4 check is present, whether or not it can be verified yet.
    assert set(checklist.CHECKS) <= set(c)


def test_the_command_prints_every_check_and_exits_nonzero_while_anything_fails(store, tmp_path):
    store.record(
        "site-a",
        {"node": "daq-1", "version": "1.17.1", "last_reading_at": _iso(10)},
        received_at=NOW - 10,
    )
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from axiom.extensions.builtins.sites.cli import main; "
            "sys.exit(main(['checklist', 'site-a']))",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "Heartbeat recent" in proc.stdout and "ok" in proc.stdout and "failing" in proc.stdout
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from axiom.extensions.builtins.sites.cli import main; "
            "sys.exit(main(['checklist', 'site-a', '--json']))",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    doc = json.loads(proc.stdout)
    assert doc["site"] == "site-a" and any(r["check"] == "Heartbeat recent" for r in doc["checks"])


def test_a_site_out_of_scope_is_not_disclosed(store, monkeypatch):
    monkeypatch.setenv("AXIOM_SERVED_SITES", "site-a")
    store.record("site-b", {"node": "theirs"}, received_at=NOW - 10)
    with pytest.raises(LookupError):
        checklist.run("site-b", now=NOW)
