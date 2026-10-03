# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Subprocess smoke tests for ``axi secrets`` CLI.

Per the cli-subprocess-smoke discipline memo: every CLI verb gets an E2E
test that runs ``python -m <module>`` as a subprocess and asserts on
stdout. Unit + Python-API tests don't catch entry-point bugs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


_MOD = "axiom.extensions.builtins.secrets.cli"


def _repo_src() -> str:
    """Return the ``src/`` directory of THIS checkout so the subprocess
    imports the worktree's axiom, not whatever an editable install on
    the developer's machine happens to point at."""
    # tests/ → secrets/ → builtins/ → extensions/ → axiom/ → src/
    return str(Path(__file__).resolve().parents[5])


def _run(*args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    src = _repo_src()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{src}{os.pathsep}{existing}" if existing else src
    return subprocess.run(
        [sys.executable, "-m", _MOD, *args],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def test_top_level_help_mentions_diagnose():
    cp = _run("--help")
    assert cp.returncode == 0, cp.stderr
    assert "diagnose" in cp.stdout


def test_diagnose_help_works():
    cp = _run("diagnose", "--help")
    assert cp.returncode == 0, cp.stderr
    assert "diagnose" in cp.stdout.lower() or "ref" in cp.stdout.lower()


def test_diagnose_bogus_env_ref_returns_nonzero_json():
    cp = _run("--json", "diagnose", "env://__DOES_NOT_EXIST_XYZ_ABC_DIAG__")
    assert cp.returncode != 0, (
        f"expected non-zero exit for unresolvable env ref; "
        f"stdout={cp.stdout!r} stderr={cp.stderr!r}"
    )
    # Well-formed JSON envelope on stdout.
    payload = json.loads(cp.stdout)
    assert "ok" in payload
    assert "value" in payload
    assert "errors" in payload
    assert payload["ok"] is False
    items = payload["value"]["items"]
    assert len(items) == 1
    assert items[0]["scheme"] == "env"
    # env provider IS available (env is always reachable); the failure
    # is at resolve time because the var isn't set.
    assert items[0]["registered"] is True
    assert items[0]["resolved"] is False
