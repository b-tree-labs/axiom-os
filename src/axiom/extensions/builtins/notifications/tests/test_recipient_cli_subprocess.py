# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Subprocess smokes for ``axi notifications recipient {set|show|list}``."""

from __future__ import annotations

import subprocess
import sys

CLI_MODULE = "axiom.extensions.builtins.notifications.cli"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", CLI_MODULE, *args],
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_recipient_set_smoke() -> None:
    result = _run("recipient", "set", "@bbooth", "slack=#alerts,inbox")
    assert result.returncode == 0, result.stderr
    assert "@bbooth" in result.stdout


def test_recipient_show_missing_smoke() -> None:
    result = _run("recipient", "show", "@nope")
    assert result.returncode == 1
    combined = result.stderr + result.stdout
    assert "@nope" in combined


def test_recipient_list_smoke() -> None:
    result = _run("recipient", "list")
    assert result.returncode == 0
    assert "recipient profile" in result.stdout.lower()


def test_recipient_set_validates_handle() -> None:
    result = _run("recipient", "set", "bbooth", "inbox")
    assert result.returncode == 1
    combined = result.stderr + result.stdout
    assert "'@'" in combined
