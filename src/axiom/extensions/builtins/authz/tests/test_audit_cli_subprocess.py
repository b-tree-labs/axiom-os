# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""End-to-end subprocess smoke for ``axi audit`` — per the
``feedback_cli_subprocess_smoke_required`` discipline memo.

These tests run ``python -m axiom.extensions.builtins.authz.cli`` as a
real subprocess so we catch entry-point + argparse + skill-registry
wiring bugs that pure-unit tests miss.
"""

from __future__ import annotations

import json
import subprocess
import sys


_MOD = "axiom.extensions.builtins.authz.cli"


def _run(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", _MOD, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_help_shows_all_audit_verbs():
    r = _run("--help")
    assert r.returncode == 0
    for verb in ("list", "show", "chain", "causes", "graduation", "explain"):
        assert verb in r.stdout, f"--help missing {verb}"


def test_explain_requires_receipt_id():
    r = _run("explain")
    assert r.returncode == 2
    assert "receipt_id" in r.stderr.lower() or "required" in r.stderr.lower()


def test_chain_requires_receipt_id():
    r = _run("chain")
    assert r.returncode == 2  # argparse missing positional
    assert "receipt_id" in r.stderr.lower() or "required" in r.stderr.lower()


def test_causes_requires_fragment_id():
    r = _run("causes")
    assert r.returncode == 2
    assert "fragment_id" in r.stderr.lower() or "required" in r.stderr.lower()


def test_graduation_help_shows_filter_flags():
    r = _run("graduation", "--help")
    assert r.returncode == 0
    for flag in ("--actor", "--intent-class", "--only-graduated",
                 "--only-proposing", "--limit"):
        assert flag in r.stdout, f"graduation --help missing {flag}"


def test_list_help_shows_filter_flags():
    r = _run("list", "--help")
    assert r.returncode == 0
    for flag in ("--since", "--primitive", "--actor", "--decision", "--limit"):
        assert flag in r.stdout, f"--help missing {flag}"


def test_show_requires_receipt_id():
    r = _run("show")
    # argparse exits 2 on missing positional.
    assert r.returncode == 2
    assert "receipt_id" in r.stderr.lower() or "required" in r.stderr.lower()


def test_no_verb_exits_nonzero():
    r = _run()
    assert r.returncode != 0


def test_list_json_invalid_since_emits_structured_error():
    r = _run("--json", "list", "--since", "garbage")
    # The CLI translates the skill's error into a non-zero exit + a
    # well-formed JSON envelope on stdout.
    assert r.returncode != 0
    payload = json.loads(r.stdout)
    assert payload["ok"] is False
    assert any("--since" in e for e in payload["errors"])
