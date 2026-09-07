# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext test`` — pytest wrapper scoped to an extension.

The test file is named ``test_test_verb.py`` rather than ``test_test.py`` to
avoid any ambiguity about who is testing whom.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from axiom.cli.ext.commands.test_verb import TestProvider, run_pytest
from axiom.cli.ext.provider import CliContext
from axiom.cli.ext.templates import scaffold as _scaffold


@pytest.fixture
def scaffolded(tmp_path: Path) -> Path:
    _scaffold.create(
        tmp_path / "test_ext",
        name="test_ext",
        owner="b-tree-labs",
        license="Apache-2.0",
        description="Test wrapper fixture",
    )
    return tmp_path / "test_ext"


def test_run_pytest_returns_zero_on_empty_tree_with_a_trivial_test(
    scaffolded: Path,
) -> None:
    """The scaffold's unit_tests folder exists — add a trivial passing test
    so pytest collection succeeds and the wrapper returns 0."""
    (scaffolded / "tests" / "unit_tests" / "test_trivial.py").write_text(
        "# Copyright (c) 2026 The University of Texas at Austin\n# Copyright (c) 2026 B-Tree Labs\n# SPDX-License-Identifier: Apache-2.0\n"
        "def test_trivial(): assert True\n",
        encoding="utf-8",
    )
    # Constrain collection to our trivial test; the scaffolded test_standard.py
    # requires `good_ext` to be importable, which is not the case in an
    # un-installed tmp scaffold — that path is covered by the validate tests.
    rc = run_pytest(
        scaffolded,
        ["tests/unit_tests/test_trivial.py"],
    )
    assert rc == 0


def test_run_pytest_returns_nonzero_on_failing_test(scaffolded: Path) -> None:
    (scaffolded / "tests" / "unit_tests" / "test_fail.py").write_text(
        "def test_fail(): assert False\n",
        encoding="utf-8",
    )
    rc = run_pytest(
        scaffolded,
        ["tests/unit_tests/test_fail.py"],
    )
    assert rc != 0


def test_run_pytest_complains_when_no_tests_dir(tmp_path: Path, capsys) -> None:
    rc = run_pytest(tmp_path / "nothing_here", [])
    assert rc != 0


def test_test_provider_forwards_pytest_args(scaffolded: Path) -> None:
    """``axi ext test -- -k <expr>`` should reach pytest as ``-k <expr>``."""
    (scaffolded / "tests" / "unit_tests" / "test_smoke.py").write_text(
        "def test_good(): assert True\n"
        "def test_broken(): raise AssertionError('filtered out')\n",
        encoding="utf-8",
    )
    provider = TestProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args(
        [
            str(scaffolded),
            "--",
            "tests/unit_tests/test_smoke.py",
            "-k",
            "good",
        ]
    )
    ctx = CliContext(cwd=scaffolded)
    rc = provider.run(args, ctx)
    assert rc == 0


def test_test_provider_defaults_path_to_context_cwd(scaffolded: Path) -> None:
    (scaffolded / "tests" / "unit_tests" / "test_trivial.py").write_text(
        "def test_trivial(): assert True\n",
        encoding="utf-8",
    )
    provider = TestProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    # No path argument supplied — defaults to context.cwd (the ext root).
    args = parser.parse_args([])
    ctx = CliContext(cwd=scaffolded)
    rc = provider.run(args, ctx)
    assert rc == 0
