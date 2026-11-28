# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The repo-wide guard tests CI runs before it starts the long suite (#2441 item 1).

A guard scans the whole tree (a retired word, a customer name, a stray directory),
so no change's selected tests ever include it and it fails wherever it fails. On
2026-11-25 one took seconds to fail and 14 minutes to be reached. These run first,
in the Smoke job, in one place, from one list.
"""

from __future__ import annotations

from pathlib import Path

from axiom.extensions.builtins.release.ci_guards import CI_GUARD_TESTS, main, targets

ROOT = Path(__file__).resolve().parents[6]


def test_every_listed_guard_names_a_file_that_exists():
    for target in targets():
        assert (ROOT / target.partition("::")[0]).is_file(), target


def test_a_pattern_expands_to_the_files_it_matches_and_a_pattern_matching_nothing_is_a_failure():
    assert targets(), "an empty guard list would let Smoke pass having checked nothing"
    assert "tests/test_*_is_retired.py" in CI_GUARD_TESTS
    assert all("*" not in t for t in targets())
    for pattern in CI_GUARD_TESTS:
        if "*" in pattern:
            assert list(ROOT.glob(pattern)), f"{pattern} matches no file, so it guards nothing"


def test_every_guard_says_why_it_runs_first():
    for target, why in CI_GUARD_TESTS.items():
        assert len(why) > 20, target


def test_the_retired_word_the_naming_rule_and_the_hygiene_rules_are_in_the_list():
    listed = set(targets())
    for must in (
        "tests/test_signal_is_retired.py",
        "tests/test_axiom_names_no_domain.py",
        "tests/test_repo_hygiene.py",
        "tests/test_every_test_file_is_collected.py",
        "tests/test_ci_cost_controls.py",
        "tests/test_ci_fails_fast.py",
    ):
        assert must in listed, must


def test_a_target_is_never_listed_twice_and_never_names_a_directory():
    names = targets()
    assert len(names) == len(set(names))
    assert not any((ROOT / t).is_dir() for t in names)


def test_the_command_prints_one_target_per_line_for_the_workflow_to_pass_to_pytest(capsys):
    assert main(["--ci-targets"]) == 0
    assert capsys.readouterr().out.split() == list(targets())


def test_a_guard_needs_nothing_the_smoke_job_does_not_install():
    """Smoke installs the package and pytest, not a database. A listed guard that
    needs one would fail Smoke for a reason that has nothing to do with the change."""
    for target in targets():
        text = (ROOT / target.partition("::")[0]).read_text()
        assert "pg_only" not in text and "psycopg" not in text, target
