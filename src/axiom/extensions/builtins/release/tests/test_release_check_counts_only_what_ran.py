# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""`axi release check` counts only what ran, and a check that cannot run fails the gate.

On 2026-11-23, the day before a release decision, it printed "8/12 checks passed". Four of the
twelve were free-text advice from the learned-failure database, run as if they were commands
("Lock file + base image pinned by digest; ..."), and a pipe it cannot run. They errored, were
printed as warnings, and were counted as passed. Only four genuinely passed. A release gate
that cannot tell "passed" from "never ran" is worse than none.

- RC-1: free-text advice is listed apart, never counted as a check, and never fails the gate.
- RC-2: a command that cannot be run is "could not run": it fails the gate and is not "passed".
- RC-3: the summary names passed, findings and could-not-run; they add up to the commands.
- RC-4: the same command listed twice runs once.
- RC-5: when every command ran clean, the gate passes.
"""

from __future__ import annotations

import argparse

import pytest

from axiom.extensions.builtins.release import _legacy_rivet_cli as cli
from axiom.extensions.builtins.release import failure_patterns


@pytest.fixture
def run_check(monkeypatch, capsys):
    def go(checks, results):
        """``results`` maps a command to ``(code, text)`` or an exception to raise."""
        monkeypatch.setattr(failure_patterns.FailurePatternDB, "__init__", lambda self, *a, **k: None)
        monkeypatch.setattr(failure_patterns.FailurePatternDB, "get_prevention_checks", lambda self: list(checks))
        ran = []

        def fake(cmd):
            ran.append(cmd)
            got = results.get(cmd, (0, ""))
            if isinstance(got, Exception):
                raise got
            return got

        monkeypatch.setattr(cli, "_run_prevention", fake)
        code = cli._cmd_check(argparse.Namespace())
        return code, capsys.readouterr().out, ran

    return go


ADVICE = "Lock file + base image pinned by digest; flag base-only Dependabot bumps on unlocked services"


def test_rc1_advice_is_listed_apart_and_never_counted(run_check):
    code, out, ran = run_check(["ruff check", ADVICE], {})
    assert code == 0
    assert ADVICE not in ran, "advice was run as a command"
    assert "Advice (not a check" in out and ADVICE[:30] in out
    assert "1 passed" in out and "0 could not run" in out


def test_rc2_a_command_that_cannot_run_fails_the_gate_and_is_not_passed(run_check):
    bad = "ruff check | sort"
    code, out, _ = run_check(["ruff check", bad], {bad: ValueError("not an argument list")})
    assert code == 1
    assert "1 passed" in out and "1 could not run" in out
    assert "[could not run]" in out and bad in out


def test_rc2_a_command_that_errors_at_run_time_is_could_not_run(run_check):
    cmd = "ruff check"
    code, out, _ = run_check([cmd], {cmd: FileNotFoundError("no such program")})
    assert code == 1 and "0 passed" in out and "1 could not run" in out


def test_rc3_findings_are_counted_apart_from_passes(run_check):
    code, out, _ = run_check(["ruff check", "grep -r x ."], {"grep -r x .": (0, "a.py: x")})
    assert code == 1
    assert "1 passed, 1 finding, 0 could not run" in out


def test_rc4_a_duplicate_runs_once(run_check):
    _code, out, ran = run_check(["ruff check", "ruff check", "ruff check"], {})
    assert ran == ["ruff check"]
    assert "1 passed" in out


def test_rc5_all_clean_passes(run_check):
    code, out, _ = run_check(["ruff check", "ruff format --check"], {})
    assert code == 0 and "2 passed, 0 findings, 0 could not run" in out


def test_what_is_installed_here_never_decides_what_is_advice():
    """CI has no ruff on its PATH; the first version asked `which`, so `ruff check` became
    advice there and "Running 0 prevention checks" passed. A missing program is a check that
    could not run, not a sentence."""
    assert not cli._is_advice("ruff check --target-version py311")
    assert not cli._is_advice("a-program-nobody-has-installed --flag")
    assert cli._is_advice(ADVICE)
    assert cli._is_advice("Import/smoke test against the built image before deploy")
    assert cli._is_advice("")


def test_a_missing_program_is_could_not_run_not_advice(run_check):
    cmd = "a-program-nobody-has-installed --flag"
    code, out, ran = run_check([cmd], {cmd: FileNotFoundError("no such program")})
    assert ran == [cmd] and code == 1
    assert "1 could not run" in out and "Advice" not in out
