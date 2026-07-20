# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Subprocess smokes for the P2 verbs — ``axi memory absorb``,
``axi memory conflicts list``, ``axi memory dedup recluster`` — driven
through the real CLI entry point against an isolated state tree and a
fixture harness store.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

MODULE = "axiom.extensions.builtins.memory.cli"
PRINCIPAL = "@alice:home"

# This worktree's src must win over any editable anchor installed from a
# sibling worktree, or the subprocess exercises someone else's checkout.
_SRC_ROOT = Path(__file__).resolve().parents[6]


def _run(state_dir: Path, *argv: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["AXI_STATE_DIR"] = str(state_dir)
    env.pop("NEUT_STATE_DIR", None)
    env["PYTHONPATH"] = str(_SRC_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    return subprocess.run(
        [sys.executable, "-m", MODULE, *argv],
        capture_output=True, text=True, timeout=120, env=env,
    )


@pytest.fixture
def state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    return state


@pytest.fixture
def claude_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    memdir = home / ".claude" / "projects" / "-Users-alice-proj" / "memory"
    memdir.mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text("# Rules\n\nBe brief.\n")
    (memdir / "feedback_tdd.md").write_text(
        "---\nname: TDD\ndescription: Tests first\ntype: feedback\n---\n\n"
        "Tests before implementation.\n"
    )
    return home


def test_absorb_conflicts_recluster_end_to_end(state, claude_home):
    # 1. absorb — two memories land.
    absorb = _run(
        state, "absorb", "--harness", "claude-code",
        "--home", str(claude_home), "--principal", PRINCIPAL, "--json",
    )
    assert absorb.returncode == 0, absorb.stderr
    payload = json.loads(absorb.stdout)
    assert payload["imported"] == 2
    assert payload["harness"] == "claude-code"

    # 2. re-absorb — a no-op (idempotency through the real CLI).
    again = _run(
        state, "absorb", "--harness", "claude-code",
        "--home", str(claude_home), "--principal", PRINCIPAL, "--json",
    )
    assert again.returncode == 0, again.stderr
    payload = json.loads(again.stdout)
    assert payload["imported"] == 0
    assert payload["skipped_echo"] == 2

    # 3. plant a contradiction in the source; absorb queues a conflict.
    topic = (
        claude_home / ".claude" / "projects" / "-Users-alice-proj"
        / "memory" / "feedback_tdd.md"
    )
    topic.write_text(topic.read_text().replace(
        "Tests before implementation.", "Skip tests for prototypes.",
    ))
    drifted = _run(
        state, "absorb", "--harness", "claude-code",
        "--home", str(claude_home), "--principal", PRINCIPAL, "--json",
    )
    assert drifted.returncode == 0, drifted.stderr
    payload = json.loads(drifted.stdout)
    assert payload["conflicts_queued"] == 1

    # 4. conflicts list — read-only queue shows the kept-both pair.
    conflicts = _run(
        state, "conflicts", "list", "--principal", PRINCIPAL, "--json",
    )
    assert conflicts.returncode == 0, conflicts.stderr
    queue = json.loads(conflicts.stdout)
    assert queue["count"] == 1
    assert queue["conflicts"][0]["status"] == "open"

    # 5. dedup recluster (dry run) — runs clean; the open-conflict pair
    # is never merged.
    recluster = _run(
        state, "dedup", "recluster", "--principal", PRINCIPAL,
        "--dry-run", "--json",
    )
    assert recluster.returncode == 0, recluster.stderr
    report = json.loads(recluster.stdout)
    assert report["dry_run"] is True
    assert report["merged"] == 0


def test_absorb_unknown_harness_fails_cleanly(state):
    result = _run(
        state, "absorb", "--harness", "clippy",
        "--principal", PRINCIPAL,
    )
    assert result.returncode == 1
    assert "unknown harness" in result.stderr
