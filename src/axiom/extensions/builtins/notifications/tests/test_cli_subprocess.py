# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Subprocess smoke test for ``axi notifications {send,list,channels}``.

Per [[feedback_cli_subprocess_smoke_required]]: every CLI verb gets an E2E
test that runs the module as a subprocess and asserts on stdout — unit
tests + Python-API smokes don't catch entry-point or argparse bugs.
"""

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


def test_no_args_prints_usage() -> None:
    result = _run()
    # argparse exits 2 when a required subparser is missing.
    assert result.returncode in (1, 2)
    combined = (result.stderr + result.stdout).lower()
    assert "usage" in combined or "subcommand" in combined


def test_channels_lists_inbox() -> None:
    result = _run("channels")
    assert result.returncode == 0
    assert "inbox" in result.stdout.lower()


def test_send_to_inbox_smoke() -> None:
    result = _run(
        "send",
        "--recipient", "@jim:test",
        "--summary", "hello from CLI",
        "--classification", "internal",
    )
    assert result.returncode == 0, result.stderr
    assert "succeeded" in result.stdout.lower() or "delivered" in result.stdout.lower()


def test_list_smoke() -> None:
    result = _run("list", "--recipient", "@jim:test")
    assert result.returncode == 0
