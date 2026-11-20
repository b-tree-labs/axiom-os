# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A retired-name notice reaches stderr once, not twice (#966).

The notice is printed to stderr and also logged. With no handler configured
anywhere, ``log.warning`` does not go nowhere: Python falls back to
``logging.lastResort``, which writes WARNING to stderr, so the operator saw
every notice twice. Inside pytest a capture handler is always installed and
hides this, so the check runs in a fresh interpreter with logging untouched.
"""
from __future__ import annotations

import os
import subprocess
import sys

_SCRIPT = (
    "from axiom.infra.brand_migration import getenv\n"
    "getenv('AXIOM_HYGIENE_SCRATCH_DIR')\n"
)


def _stderr_of_a_fresh_process(extra_env: dict[str, str]) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("NEUT_")}
    env.update(extra_env)
    done = subprocess.run(
        [sys.executable, "-c", _SCRIPT], env=env, capture_output=True, text=True, timeout=120,
    )
    assert done.returncode == 0, done.stderr
    return done.stderr


def test_the_notice_reaches_stderr_exactly_once_with_no_logging_configured():
    err = _stderr_of_a_fresh_process({"NEUT_SCRATCH_DIR": "/srv/scratch"})
    lines = [line for line in err.splitlines() if "NEUT_SCRATCH_DIR" in line]
    assert len(lines) == 1, err


def test_with_a_handler_configured_the_log_line_still_goes_to_it():
    script = (
        "import logging\n"
        "seen = []\n"
        "class H(logging.Handler):\n"
        "    def emit(self, record): seen.append(record.getMessage())\n"
        "logging.getLogger('axiom').addHandler(H())\n"
        + _SCRIPT
        + "assert any('NEUT_SCRATCH_DIR' in m for m in seen), seen\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("NEUT_")}
    env["NEUT_SCRATCH_DIR"] = "/srv/scratch"
    done = subprocess.run([sys.executable, "-c", script], env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert sum("NEUT_SCRATCH_DIR" in line for line in done.stderr.splitlines()) == 1, done.stderr
