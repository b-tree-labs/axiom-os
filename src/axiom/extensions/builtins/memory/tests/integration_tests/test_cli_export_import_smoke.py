# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Subprocess smokes for ``axi memory export`` / ``import`` — the P0
acceptance-gate end-to-end: two isolated state trees on one machine
simulating a personal→work account port, driven through the real CLI
entry point (``python -m axiom.extensions.builtins.memory.cli``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

MODULE = "axiom.extensions.builtins.memory.cli"
SRC_PRINCIPAL = "@alice:personal"
DST_PRINCIPAL = "@alice:work"

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
def states(tmp_path: Path) -> tuple[Path, Path]:
    src = tmp_path / "src-state"
    dst = tmp_path / "dst-state"
    src.mkdir()
    dst.mkdir()
    sessions = src / "sessions"
    sessions.mkdir()
    (sessions / "s1.json").write_text(json.dumps({
        "session_id": "session://smoke-1",
        "principal_id": SRC_PRINCIPAL,
        "name": "personal-smoke",
    }))
    return src, dst


def test_export_import_end_to_end(states, tmp_path):
    src, dst = states
    bundle = tmp_path / "bundle.tar.gz"

    # Seed the source store through the real record verb.
    proc = _run(
        src, "record", "--principal", SRC_PRINCIPAL,
        "--tool", "claude-code",
        "--user-input", "remember I prefer tabs",
        "--assistant-output", "noted",
        "--summary", "prefers tabs",
    )
    assert proc.returncode == 0, proc.stderr

    # Export.
    proc = _run(
        src, "export", "--principal", SRC_PRINCIPAL,
        "--out", str(bundle), "--json",
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["counts"]["fragments"] == 1
    assert report["counts"]["sessions"] == 1
    assert bundle.exists()

    # Import into the destination state tree under the assumed principal.
    proc = _run(
        dst, "import", str(bundle),
        "--assume-principal", DST_PRINCIPAL, "--json",
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["imported"] == 1
    assert report["skipped_duplicate"] == 0
    assert report["sessions_imported"] == 1
    assert (dst / "sessions" / "s1.json").exists()

    # Recall parity: the destination shows the fragment for the same query.
    proc = _run(dst, "show", SRC_PRINCIPAL, "--json")
    assert proc.returncode == 0, proc.stderr
    shown = json.loads(proc.stdout)
    assert shown["fragment_count"] == 1
    assert shown["fragments"][0]["summary"] == "prefers tabs"

    # Idempotency: re-import is a no-op.
    proc = _run(
        dst, "import", str(bundle),
        "--assume-principal", DST_PRINCIPAL, "--json",
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["imported"] == 0
    assert report["skipped_duplicate"] == 1


def test_include_vault_refused_in_subprocess(states, tmp_path):
    src, _ = states
    proc = _run(
        src, "export", "--principal", SRC_PRINCIPAL,
        "--out", str(tmp_path / "b.tar.gz"), "--include-vault",
    )
    assert proc.returncode == 1
    assert "vault" in proc.stderr.lower()
