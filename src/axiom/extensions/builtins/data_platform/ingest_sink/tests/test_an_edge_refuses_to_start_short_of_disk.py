# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The edge's unit refuses to start when its data directory is short of space."""

import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "ingest-edge"
CHECK = DEPLOY / "check-free-space.sh"

pytestmark = pytest.mark.skipif(shutil.which("df") is None, reason="needs a POSIX df")


def _run(directory, min_gib):
    return subprocess.run(["sh", str(CHECK), str(directory), str(min_gib)], capture_output=True, text=True)


def test_enough_space_starts(tmp_path):
    assert _run(tmp_path, 0).returncode == 0


def test_too_little_space_refuses_with_the_fix(tmp_path):
    out = _run(tmp_path, 10**9)
    assert out.returncode == 1 and "required" in out.stderr


def test_the_unit_runs_the_check_before_the_edge():
    unit = (DEPLOY / "axiom-ingest-edge.service").read_text(encoding="utf-8")
    assert "ExecStartPre=/opt/axiom-edge/check-free-space.sh /var/lib/axiom-edge" in unit
    assert "AXIOM_EDGE_MIN_FREE_GIB" in (DEPLOY / "edge.env.example").read_text(encoding="utf-8")
