# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Every daemon agent's `heartbeat_command` must be a valid invocation of its
own CLI.

The background service dispatches `axi <heartbeat_command>` on each interval; a
typo or an undefined flag (e.g. `publish status --json`, which has no `--json`
subparser) makes the agent argparse-fail on *every* tick — silent on the
operator's machine, loud on Austin's Windows host (2026-05-28). Unit tests and
in-process smokes miss this because the failure only manifests at the
entry-point/argparse boundary, so we run each heartbeat as a real subprocess.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve()
_BUILTINS = _TESTS.parents[2]          # .../extensions/builtins
_SRC_ROOT = _TESTS.parents[5]          # .../src  (dir containing `axiom`)

_REJECTIONS = ("unrecognized arguments", "invalid choice", "no such option")


def _discover_heartbeats() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for manifest in sorted(_BUILTINS.glob("*/axiom-extension.toml")):
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        cmd = (data.get("agent") or {}).get("heartbeat_command")
        if cmd:
            found.append((manifest.parent.name, cmd))
    return found


def _discover_daemon_intervals() -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for manifest in sorted(_BUILTINS.glob("*/axiom-extension.toml")):
        agent = tomllib.loads(manifest.read_text(encoding="utf-8")).get("agent") or {}
        # Only always-on agents spawn a heartbeat subprocess on a fixed cadence.
        if agent.get("startup") in ("daemon", "eager") and agent.get("heartbeat_command"):
            out.append((manifest.parent.name, int(agent.get("heartbeat_interval", 0))))
    return out


_HEARTBEATS = _discover_heartbeats()
_DAEMON_INTERVALS = _discover_daemon_intervals()

# Floor for always-on heartbeats: a tick spawns a full subprocess, so anything
# under a minute is a host-flooding misconfig (publishing was 10s -> ~8.6k/day).
_MIN_DAEMON_INTERVAL_SECS = 60


def test_some_heartbeats_exist():
    # Guard against a discovery regression silently emptying the matrix.
    assert _HEARTBEATS, "no [agent].heartbeat_command found under builtins/"


@pytest.mark.parametrize(
    "ext,interval", _DAEMON_INTERVALS, ids=[e for e, _ in _DAEMON_INTERVALS]
)
def test_daemon_heartbeat_interval_above_floor(ext: str, interval: int):
    assert interval >= _MIN_DAEMON_INTERVAL_SECS, (
        f"{ext} heartbeat fires every {interval}s — under the {_MIN_DAEMON_INTERVAL_SECS}s "
        f"floor; a full subprocess that often floods the host"
    )


@pytest.mark.parametrize("ext,cmd", _HEARTBEATS, ids=[e for e, _ in _HEARTBEATS])
def test_heartbeat_command_parses(ext: str, cmd: str, tmp_path):
    env = {**os.environ, "PYTHONPATH": str(_SRC_ROOT), "AXIOM_DISABLE_SELF_HEAL": "1"}
    proc = subprocess.run(
        [sys.executable, "-m", "axiom.axiom_cli", *cmd.split()],
        cwd=tmp_path,            # context-free: a heartbeat must run anywhere
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )
    blob = (proc.stdout + proc.stderr).lower()
    bad = [r for r in _REJECTIONS if r in blob]
    assert not bad, (
        f"heartbeat `{cmd}` for agent '{ext}' was rejected by its CLI "
        f"({bad}); rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    )
