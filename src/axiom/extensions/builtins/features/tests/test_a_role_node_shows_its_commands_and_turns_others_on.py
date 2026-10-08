# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The command line a person on a role node actually meets.

Run as a real process against a real config file: help lists the role's
commands, a hidden command refuses with the line that turns it on, that line
works, and turning it off again hides it. Nothing is installed or removed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "axiom.axiom_cli", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    node = tmp_path / "node.toml"
    node.write_text('[node]\nrole = "assistant-only"\nfunctions = ["assistant"]\nfeatures = []\n')
    e = dict(os.environ)
    e.update(
        {
            "AXIOM_NODE_CONFIG": str(node),
            "AXI_STATE_DIR": str(tmp_path / "state"),
            "AXIOM_DISABLE_UPDATE_NUDGE": "1",
            "AXIOM_DISABLE_SELF_HEAL": "1",
            "NO_COLOR": "1",
        }
    )
    return e


def test_help_lists_the_role_and_its_commands(env):
    out = _run(["--help"], env).stdout
    assert "This node's role: assistant-only (assistant)." in out
    assert "  chat" in out
    assert "  data " not in out and "  serve" not in out
    assert "features" in out  # how to turn things on is always visible


def test_a_hidden_command_says_how_to_turn_it_on_and_that_works(env):
    refused = _run(["data", "--help"], env)
    assert refused.returncode == 2
    assert "features enable data_platform" in refused.stdout

    on = _run(["features", "enable", "data_platform"], env)
    assert on.returncode == 0, on.stderr
    assert _run(["data", "--help"], env).returncode == 0

    off = _run(["features", "disable", "data_platform"], env)
    assert off.returncode == 0, off.stderr
    assert _run(["data", "--help"], env).returncode == 2


def test_the_role_s_own_function_cannot_be_switched_off_as_a_feature(env):
    res = _run(["features", "disable", "assistant"], env)
    assert res.returncode != 0
    assert "part of this node's role" in res.stderr


def test_clearing_the_role_shows_everything(env):
    assert _run(["features", "role", "clear"], env).returncode == 0
    assert _run(["data", "--help"], env).returncode == 0
    assert "This node's role" not in _run(["--help"], env).stdout
