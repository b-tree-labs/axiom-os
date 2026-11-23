# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The post-merge hook: advisory lane-reclaim, propose-only, never blocks.

The reclaim logic is tested next door; this proves the shell glue the hook
adds — that it surfaces a reclaimable lane after a merge, stays silent when
there is nothing to say, respects the opt-out, and under no path fails the
merge (exit 0 always).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# repo root = .../axiom-wt-lane (src/axiom/extensions/builtins/lane/tests/<me>)
_REPO = Path(__file__).resolve().parents[6]
_HOOK = _REPO / "scripts" / "hooks" / "post-merge"
_SRC = _REPO / "src"


pytestmark = pytest.mark.skipif(not _HOOK.exists(), reason="post-merge hook not present")


def _env_with_axi(tmp_path: Path, lanes: dict) -> tuple[dict, Path]:
    """A PATH whose `axi` runs THIS checkout's hygiene CLI (what a correctly
    set-up worktree has), plus a lane registry the hook will read."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "axi"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        "shift\n"  # drop the noun 'hygiene'
        f'exec env PYTHONPATH="{_SRC}" "{sys.executable}" -c '
        "'import sys; from axiom.extensions.builtins.hygiene import cli; "
        "sys.exit(cli.main(sys.argv[1:]))' \"$@\"\n"
    )
    shim.chmod(0o755)

    lanes_file = tmp_path / "lanes.json"
    lanes_file.write_text(json.dumps({"lanes": lanes}))

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["AXIOM_LANES_FILE"] = str(lanes_file)
    env["AXI_WORKSPACE_ROOT"] = str(tmp_path)
    return env, lanes_file


def _run(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(_HOOK)], env=env, capture_output=True, text=True, timeout=60
    )


def test_a_reclaimable_lane_is_surfaced_after_merge(tmp_path):
    gone = {
        "gone-lane": {"name": "gone-lane", "front": 8810, "api": 8811,
                      "database": "axiom_lane_gone", "branch": "feat/gone",
                      "root": str(tmp_path / "vanished")},
    }
    env, _ = _env_with_axi(tmp_path, gone)
    r = _run(env)

    assert r.returncode == 0
    assert "reclaimable" in r.stdout
    assert "gone-lane" in r.stdout
    assert "axi lane release gone-lane" in r.stdout
    # Propose-only: the irreversible drop is printed, labelled, and NOT run.
    assert "dropdb axiom_lane_gone" in r.stdout and "irreversible" in r.stdout


def test_nothing_to_reclaim_is_silent(tmp_path):
    env, _ = _env_with_axi(tmp_path, {})
    r = _run(env)

    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_the_opt_out_silences_it(tmp_path):
    gone = {
        "gone-lane": {"name": "gone-lane", "front": 8810, "api": 8811,
                      "database": "axiom_lane_gone", "branch": "feat/gone",
                      "root": str(tmp_path / "vanished")},
    }
    env, _ = _env_with_axi(tmp_path, gone)
    env["AXI_NO_RECLAIM_HINT"] = "1"
    r = _run(env)

    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_it_never_fails_the_merge_even_with_no_cli(tmp_path):
    """No axi, no neut, and a python that cannot import axiom: still exit 0."""
    lanes_file = tmp_path / "lanes.json"
    lanes_file.write_text(json.dumps({"lanes": {}}))
    env = dict(os.environ)
    # A PATH with a python but no axiom on it — the fallback import fails, and
    # a reclaim hint must never be the thing that breaks a pull.
    env["PATH"] = f"{os.path.dirname(sys.executable)}:/usr/bin:/bin"
    env.pop("PYTHONPATH", None)
    env["AXIOM_LANES_FILE"] = str(lanes_file)
    r = _run(env)

    assert r.returncode == 0
