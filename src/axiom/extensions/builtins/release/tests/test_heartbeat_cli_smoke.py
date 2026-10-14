# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Subprocess smoke for `axi release heartbeat`.

Closes the test-gap that hid the 2026-06-01 AEOS-manifest regression:
the heartbeat block was missing its `path` field, so extension boot
crashed during skill registration and the launchd-fired
`axi release heartbeat` exited non-zero silently. Per
`feedback_cli_subprocess_smoke_required`, every CLI verb needs an
end-to-end subprocess test that runs the entry point and asserts on
the real exit code — unit + Python-API tests miss boot-time failures.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_release_heartbeat_subprocess_boots_and_reports(tmp_path: Path) -> None:
    """A healthy boot writes its signal entry. That is the whole assertion.

    The regression this smoke exists for is a BOOT CRASH during skill
    registration: the command dies before doing any work, so no entry is
    written. `heartbeat.jsonl` is therefore the quantity that is free to vary
    between a healthy run and the regression — and the exit code is not.

    Asserting exit 0 made this a live mirror of the world's CI state, which is
    how one red PR anywhere blocked every push on this machine. Widening to
    (0, 2) did not fix it: a healthy boot ALSO exits 1 when it finds a PR whose
    checks flipped red — observed 2026-09-08, exit 1, no traceback, entry
    written, reporting another PR's failing job. That is the command working,
    not failing, so the exit code cannot carry the assertion.

    `AXI_STATE_DIR` is redirected so the smoke stops writing into the
    developer's real `~/.axi` tree, and so the entry it asserts on is
    unambiguously the one this run produced.
    """
    state = tmp_path / "state"
    result = subprocess.run(
        [sys.executable, "-m", "axiom.extensions.builtins.release", "heartbeat"],
        capture_output=True,
        text=True,
        timeout=120,
        env={**_clean_env(), "AXI_STATE_DIR": str(state)},
        cwd=tmp_path,
    )

    # A boot crash is a traceback, and it is the thing this test guards.
    assert "Traceback" not in result.stderr, (
        f"heartbeat crashed on boot\n--- stderr ---\n{result.stderr}"
    )
    # Killed by a signal (segfault, OOM) is never healthy.
    assert result.returncode >= 0, f"heartbeat died on signal {-result.returncode}"

    log = state / "agents" / "rivet" / "heartbeat.jsonl"
    assert log.exists(), (
        "heartbeat wrote no signal entry — the boot-crash signature\n"
        f"exit={result.returncode}\n--- stdout ---\n{result.stdout[:1500]}\n"
        f"--- stderr ---\n{result.stderr[:1500]}"
    )
    lines = [ln for ln in log.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected exactly one entry from one run, got {len(lines)}"

    entry = json.loads(lines[0])
    assert entry["agent"] == "rivet"
    assert entry["ts"], "entry carries no timestamp"


def _repo_src() -> str:
    """The `src/` tree THIS test file lives in.

    The venv's editable install is anchored to one checkout, so a subprocess
    started from any other worktree imports that anchor's code instead of the
    source under test — a mutation to this branch's heartbeat changed nothing,
    and the smoke passed against a different checkout entirely. Pinning
    PYTHONPATH makes the subprocess exercise the branch it is testing, in CI
    and in every worktree.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if parent.name == "src":
            return str(parent)
    raise AssertionError(f"no src/ ancestor for {here}")


def _clean_env() -> dict[str, str]:
    """Inherit the real environment minus any state-dir override already set,
    with this checkout's `src` ahead of anything inherited on PYTHONPATH.

    A developer (or an outer fixture) exporting `AXI_STATE_DIR` would otherwise
    win over the one this test sets, and the assertion would read someone
    else's file.
    """
    import os

    env = {k: v for k, v in os.environ.items() if not k.endswith("_STATE_DIR")}
    inherited = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{_repo_src()}{os.pathsep}{inherited}" if inherited else _repo_src()
    )
    return env


def test_release_heartbeat_help_subprocess_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "axiom.extensions.builtins.release", "heartbeat", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"--help exited {result.returncode}\nstderr: {result.stderr}"
    assert "heartbeat" in result.stdout.lower()
