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

import subprocess
import sys


def test_release_heartbeat_subprocess_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "axiom.extensions.builtins.release", "heartbeat"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"axi release heartbeat exited {result.returncode}\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def test_release_heartbeat_help_subprocess_exits_zero() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "axiom.extensions.builtins.release",
         "heartbeat", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"--help exited {result.returncode}\nstderr: {result.stderr}"
    )
    assert "heartbeat" in result.stdout.lower()
