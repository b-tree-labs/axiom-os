# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Two defects another session hit while rehearsing a deploy (2026-11-22).

- SI-1: ``log seed --scale N`` without ``--json`` printed nothing useful and failed with
  "'str' object has no attribute 'get'" after writing the rows: the text printer took the
  seed's list of log ids for the declared-logs listing. It now prints what was seeded.
- SI-2: ``import axiom.attest.verify`` in a fresh interpreter failed on a circular import
  (``_site`` -> attest skills -> filters -> ``axiom.attest.query`` -> ``_site``). Every
  module of the ``axiom.attest`` library now imports first, on its own.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from axiom.extensions.builtins.attest import log_cli
from axiom.infra.skills import SkillResult

_SRC = Path(__file__).resolve().parents[5]


def test_si1_seed_text_output_says_what_was_seeded(capsys):
    value = {"logbooks": ["ops", "maint"], "database": "lane_db", "seed": 0, "seals_per_log": 3,
             "declared": "/tmp/x/.axi/extensions"}  # fmt: skip
    said = ["seeded 10 fictional entries across 2 logs at s1 in lane_db (seed 0; 3 sealed periods per log)",
            "declared the logs in /tmp/x/.axi/extensions (restart the dev node to list them)"]  # fmt: skip
    code = log_cli._emit(SkillResult(ok=True, value=value, actions_taken=said), as_json=False)
    out = capsys.readouterr().out
    assert code == 0
    assert said[0] in out and said[1] in out


@pytest.mark.parametrize("module", ["verify", "query", "_site", "chain", "sign", "drafts", "confirm", "anchor"])
def test_si2_each_library_module_imports_first_in_a_fresh_interpreter(module):
    env = {**os.environ, "PYTHONPATH": str(_SRC)}
    done = subprocess.run(
        [sys.executable, "-c", f"import axiom.attest.{module}"],
        capture_output=True, text=True, env=env, timeout=120, check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr[-800:]
