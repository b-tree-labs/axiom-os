# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``scripts/lint_adr_numbers.py``.

This lint prevents the ADR-number collision pattern that has hit twice
in one week (047 → 048, then 051 → 052) where two parallel sessions both
grab the same next-number unbeknownst to each other.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Load the script as a module (scripts/ isn't a package).
_SCRIPT = Path(__file__).parent.parent / "scripts" / "lint_adr_numbers.py"
_spec = importlib.util.spec_from_file_location("lint_adr_numbers", _SCRIPT)
assert _spec and _spec.loader
lint_adr_numbers = importlib.util.module_from_spec(_spec)
sys.modules["lint_adr_numbers"] = lint_adr_numbers
_spec.loader.exec_module(lint_adr_numbers)


def _write_adr(d: Path, number: int, slug: str) -> Path:
    p = d / f"adr-{number:03d}-{slug}.md"
    p.write_text(f"# ADR-{number:03d}: {slug}\n")
    return p


def test_clean_dir_lints_zero(tmp_path):
    _write_adr(tmp_path, 1, "first")
    _write_adr(tmp_path, 2, "second")
    _write_adr(tmp_path, 47, "forty-seventh")
    assert lint_adr_numbers.lint(tmp_path) == 0


def test_collision_lints_nonzero(tmp_path):
    _write_adr(tmp_path, 51, "cross-provider-context")
    _write_adr(tmp_path, 51, "database-tenancy")
    assert lint_adr_numbers.lint(tmp_path) == 1


def test_collision_message_names_both_files(tmp_path, capsys):
    _write_adr(tmp_path, 47, "one")
    _write_adr(tmp_path, 47, "two")
    lint_adr_numbers.lint(tmp_path)
    err = capsys.readouterr().err
    assert "ADR-047" in err
    assert "adr-047-one.md" in err
    assert "adr-047-two.md" in err


def test_multiple_collisions_all_reported(tmp_path, capsys):
    _write_adr(tmp_path, 51, "a")
    _write_adr(tmp_path, 51, "b")
    _write_adr(tmp_path, 60, "c")
    _write_adr(tmp_path, 60, "d")
    lint_adr_numbers.lint(tmp_path)
    err = capsys.readouterr().err
    assert "ADR-051" in err and "ADR-060" in err


def test_next_available_empty_returns_1(tmp_path):
    assert lint_adr_numbers.next_available([]) == 1


def test_next_available_returns_max_plus_one(tmp_path):
    _write_adr(tmp_path, 1, "a")
    _write_adr(tmp_path, 50, "b")
    _write_adr(tmp_path, 47, "c")
    adrs = lint_adr_numbers.find_adrs(tmp_path)
    assert lint_adr_numbers.next_available(adrs) == 51


def test_next_available_does_not_fill_gaps(tmp_path):
    # ADR-049 was deleted; --next still returns 51, not 49.
    _write_adr(tmp_path, 48, "a")
    _write_adr(tmp_path, 50, "b")
    adrs = lint_adr_numbers.find_adrs(tmp_path)
    assert lint_adr_numbers.next_available(adrs) == 51


def test_find_adrs_ignores_non_adr_files(tmp_path):
    _write_adr(tmp_path, 1, "real")
    (tmp_path / "README.md").write_text("not an ADR")
    (tmp_path / "adr-template.md").write_text("template, not numbered")
    (tmp_path / "adr-01-too-short.md").write_text("2-digit not allowed")
    adrs = lint_adr_numbers.find_adrs(tmp_path)
    assert len(adrs) == 1
    assert adrs[0][0] == 1


def test_main_cli_next_prints_number(tmp_path, capsys):
    _write_adr(tmp_path, 51, "x")
    rc = lint_adr_numbers.main(["--next", "--adr-dir", str(tmp_path)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "052"


def test_main_cli_lint_default_exits_nonzero_on_collision(tmp_path):
    _write_adr(tmp_path, 1, "a")
    _write_adr(tmp_path, 1, "b")
    rc = lint_adr_numbers.main(["--adr-dir", str(tmp_path)])
    assert rc == 1


def test_real_repo_adr_dir_is_clean():
    """The actual ``docs/adrs/`` in this repo must lint clean."""
    repo_adr_dir = Path(__file__).parent.parent / "docs" / "adrs"
    assert lint_adr_numbers.lint(repo_adr_dir) == 0
