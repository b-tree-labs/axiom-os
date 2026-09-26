# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext validate`` — deeper AEOS checks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest
import tomlkit

from axiom.cli.ext.commands.validate import (
    ValidateProvider,
    validate_extension,
)
from axiom.cli.ext.provider import CliContext
from axiom.cli.ext.templates import scaffold as _scaffold

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def good_ext(tmp_path: Path) -> Path:
    """Fresh scaffold — manifest provides block and pyproject entry point
    are aligned from day one (the scaffold's placeholder cmd).

    Tests that want to exercise a *missing* entry point call
    :func:`_unalign_entry_points` to drop the placeholder mapping.
    """
    _scaffold.create(
        tmp_path / "good_ext",
        name="good_ext",
        owner="b-tree-labs",
        license="Apache-2.0",
        description="A good ext",
    )
    return tmp_path / "good_ext"


def _unalign_entry_points(ext_path: Path, name: str = "good_ext") -> None:
    """Drop the scaffold's placeholder entry point to create a manifest/pyproject gap."""
    pyproj = ext_path / "pyproject.toml"
    doc = tomlkit.parse(pyproj.read_text())
    ep_table = doc.get("project", {}).get("entry-points", {}).get("axiom.commands")
    if ep_table is not None and name in ep_table:
        del ep_table[name]
        pyproj.write_text(tomlkit.dumps(doc), encoding="utf-8")


# ---------------------------------------------------------------------------
# Entry-point resolution
# ---------------------------------------------------------------------------


def test_validate_flags_missing_entry_point(good_ext: Path) -> None:
    _unalign_entry_points(good_ext)
    results = validate_extension(good_ext)
    failed = [r for r in results if not r.ok]
    assert any("entry_point" in r.check for r in failed)


def test_validate_passes_when_entry_point_aligned(good_ext: Path) -> None:
    results = validate_extension(good_ext)
    ep_checks = [r for r in results if r.check.startswith("entry_point")]
    assert ep_checks, "expected at least one entry_point check"
    assert all(r.ok for r in ep_checks), f"entry-point checks failed: {ep_checks}"


def test_validate_flags_entry_point_mismatch(good_ext: Path) -> None:
    """If pyproject declares a different target, validate must fail loudly."""
    pyproj = good_ext / "pyproject.toml"
    doc = tomlkit.parse(pyproj.read_text())
    doc.setdefault("project", tomlkit.table()).setdefault(
        "entry-points", tomlkit.table()
    )
    doc["project"]["entry-points"].setdefault("axiom.commands", tomlkit.table())
    doc["project"]["entry-points"]["axiom.commands"]["good_ext"] = "wrong.module:symbol"
    pyproj.write_text(tomlkit.dumps(doc), encoding="utf-8")

    results = validate_extension(good_ext)
    mismatches = [r for r in results if not r.ok and "entry_point" in r.check]
    assert mismatches
    assert any("does not match" in r.detail for r in mismatches)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def test_validate_public_api_passes_on_empty_all(good_ext: Path) -> None:
    results = validate_extension(good_ext)
    api = [r for r in results if r.check == "public_api"]
    assert api and api[0].ok


def test_validate_public_api_flags_missing_symbol(good_ext: Path) -> None:
    init = good_ext / "good_ext" / "__init__.py"
    init.write_text(
        "# Copyright (c) 2026 The University of Texas at Austin\n# Copyright (c) 2026 B-Tree Labs\n# SPDX-License-Identifier: Apache-2.0\n"
        '__all__ = ["Missing"]\n'
    )
    results = validate_extension(good_ext)
    api = [r for r in results if r.check == "public_api"]
    assert api and not api[0].ok
    assert "Missing" in api[0].detail


def test_validate_public_api_flags_non_literal_all(good_ext: Path) -> None:
    init = good_ext / "good_ext" / "__init__.py"
    init.write_text(
        "# Copyright (c) 2026 The University of Texas at Austin\n# Copyright (c) 2026 B-Tree Labs\n# SPDX-License-Identifier: Apache-2.0\n"
        "import sys\n"
        "__all__ = sys.modules.keys()\n"
    )
    results = validate_extension(good_ext)
    api = [r for r in results if r.check == "public_api"]
    assert api and not api[0].ok


# ---------------------------------------------------------------------------
# Forbidden imports (grep-based, pending import-linter integration)
# ---------------------------------------------------------------------------


def test_validate_flags_private_axiom_import(good_ext: Path) -> None:
    offender = good_ext / "good_ext" / "_offender.py"
    offender.write_text(
        "# Copyright (c) 2026 The University of Texas at Austin\n# Copyright (c) 2026 B-Tree Labs\n# SPDX-License-Identifier: Apache-2.0\n"
        "from axiom.memory._internal import something  # forbidden\n"
    )
    results = validate_extension(good_ext)
    forbidden = [r for r in results if r.check == "forbidden_imports"]
    assert forbidden and not forbidden[0].ok


def test_validate_clean_imports_pass(good_ext: Path) -> None:
    results = validate_extension(good_ext)
    forbidden = [r for r in results if r.check == "forbidden_imports"]
    assert forbidden and forbidden[0].ok


# ---------------------------------------------------------------------------
# Provider: CLI surface + JSON output
# ---------------------------------------------------------------------------


def test_validate_provider_exit_code_on_failure(good_ext: Path) -> None:
    _unalign_entry_points(good_ext)
    provider = ValidateProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    # Skip the pytest subprocess to keep the test fast and hermetic; the
    # in-process check is exercised by test_test_verb.py.
    args = parser.parse_args([str(good_ext), "--skip-tests"])
    ctx = CliContext(cwd=good_ext)
    rc = provider.run(args, ctx)
    assert rc != 0


def test_validate_provider_success_on_clean_extension(good_ext: Path) -> None:
    provider = ValidateProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args([str(good_ext), "--skip-tests"])
    ctx = CliContext(cwd=good_ext)
    rc = provider.run(args, ctx)
    assert rc == 0


def test_validate_provider_json_output(good_ext: Path, capsys) -> None:
    provider = ValidateProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args([str(good_ext), "--skip-tests", "--json"])
    ctx = CliContext(cwd=good_ext)
    rc = provider.run(args, ctx)
    out = capsys.readouterr().out
    data = json.loads(out)
    assert "results" in data and isinstance(data["results"], list)
    assert "failed_count" in data
    assert rc == (1 if data["failed_count"] else 0)


# ---------------------------------------------------------------------------
# Preflight: missing manifest/pyproject
# ---------------------------------------------------------------------------


def test_validate_preflight_fails_for_missing_files(tmp_path: Path) -> None:
    results = validate_extension(tmp_path / "nothing_here")
    assert results and not results[0].ok
    assert results[0].check == "preflight"
