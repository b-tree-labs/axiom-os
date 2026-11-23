# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI subprocess smoke tests.

Per `feedback_cli_subprocess_smoke_required`: every CLI verb needs an
E2E test that runs ``python -m`` as a subprocess and asserts on stdout.
Unit tests + Python-API smokes do not catch entry-point bugs or
verb-to-verb format drift.

PULSE-1 ships seven verbs; this file exercises each one in subprocess.
``list`` returns ok=True (empty inventory); the rest are expected to
exit non-zero with a clear PULSE-1-wiring-in-progress message — the
test asserts on the message shape, not on success.
"""

from __future__ import annotations

import json
import subprocess
import sys


def _run(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "axiom.extensions.builtins.schedule.cli", *argv],
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_cli_list_returns_zero_with_empty_inventory():
    r = _run("--json", "list")
    assert r.returncode == 0, r.stderr
    payload = json.loads(r.stdout)
    assert payload["ok"] is True
    assert payload["value"]["schedules"] == []


def test_cli_register_requires_cadence_and_action():
    r = _run("register", "--cadence", "interval:1h", "--action", "x.y")
    # Stub returns ok=False with the PULSE-1 wiring-in-progress message.
    assert r.returncode == 1
    assert "PULSE-1 wiring" in r.stderr


def test_cli_pause_requires_reason():
    r = _run("pause", "sched-abc", "--reason", "operator pause")
    assert r.returncode == 1
    assert "PULSE-1 wiring" in r.stderr


def test_cli_resume_smoke():
    r = _run("resume", "sched-abc")
    assert r.returncode == 1
    assert "PULSE-1 wiring" in r.stderr


def test_cli_cancel_smoke():
    r = _run("cancel", "sched-abc")
    assert r.returncode == 1
    assert "PULSE-1 wiring" in r.stderr


def test_cli_fire_now_smoke():
    r = _run("fire-now", "sched-abc")
    assert r.returncode == 1
    assert "PULSE-1 wiring" in r.stderr


def test_cli_status_smoke():
    r = _run("status", "sched-abc")
    assert r.returncode == 1
    assert "PULSE-1 wiring" in r.stderr


def test_cli_unknown_verb_exits_nonzero():
    r = _run("teleport", "sched-abc")
    assert r.returncode != 0
