# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext lint`` — Bronze-level AEOS conformance."""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path

import pytest

from axiom.cli.ext.commands.init import InitProvider
from axiom.cli.ext.commands.lint import LintProvider, lint_extension
from axiom.cli.ext.provider import CliContext
from axiom.cli.ext.templates import scaffold as _scaffold


@pytest.fixture
def good_ext(tmp_path: Path) -> Path:
    """Produce a freshly-scaffolded extension that must pass lint cleanly."""
    _scaffold.create(
        tmp_path / "good_ext",
        name="good_ext",
        owner="b-tree-labs",
        license="Apache-2.0",
        description="A good ext",
    )
    return tmp_path / "good_ext"


@pytest.fixture
def run_lint(tmp_path: Path):
    """Invoke LintProvider.run and capture its returncode."""

    def _run(path: Path, *, json_out: bool = False) -> int:
        provider = LintProvider()
        parser = argparse.ArgumentParser()
        provider.add_arguments(parser)
        argv = [str(path)] + (["--json"] if json_out else [])
        args = parser.parse_args(argv)
        ctx = CliContext(cwd=tmp_path)
        return provider.run(args, ctx)

    return _run


# ---------------------------------------------------------------------------
# Happy path: a freshly-initialized scaffold must pass Bronze lint
# ---------------------------------------------------------------------------


def test_freshly_scaffolded_extension_passes_lint(good_ext: Path) -> None:
    findings = lint_extension(good_ext)
    errors = [f for f in findings if f.severity == "error"]
    assert not errors, f"fresh scaffold failed lint: {errors}"


def test_init_plus_lint_passes_end_to_end(tmp_path: Path, run_lint) -> None:
    """The scripted flow from the spec: `axi ext init X && axi ext lint X`."""
    provider = InitProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args(["test_ext", "--dir", str(tmp_path)])
    ctx = CliContext(cwd=tmp_path)
    assert provider.run(args, ctx) == 0
    assert run_lint(tmp_path / "test_ext") == 0


def test_lint_ok_line_spells_out_bronze_meaning(
    good_ext: Path, run_lint, capsys
) -> None:
    """The success banner clarifies what Bronze means without a spec lookup."""
    assert run_lint(good_ext) == 0
    out = capsys.readouterr().out
    assert "Bronze — layout + manifest" in out


def test_lint_help_mentions_bronze_meaning() -> None:
    """`axi ext lint --help` explains Bronze inline."""
    from axiom.cli.ext.commands.lint import LintProvider

    parser = argparse.ArgumentParser()
    LintProvider().add_arguments(parser)
    parser.format_help()
    # The description is on the parser itself via the module docstring; the
    # provider carries a one-liner that now spells Bronze out.
    assert "Bronze" in LintProvider.description
    assert "layout" in LintProvider.description
    assert "manifest" in LintProvider.description


# ---------------------------------------------------------------------------
# Fixture-driven violations
# ---------------------------------------------------------------------------


def test_lint_fails_for_missing_changelog(good_ext: Path, run_lint) -> None:
    (good_ext / "CHANGELOG.md").unlink()
    findings = lint_extension(good_ext)
    codes = [f.code for f in findings]
    assert "AEOS010" in codes
    assert run_lint(good_ext) != 0


def test_lint_fails_for_missing_readme(good_ext: Path) -> None:
    (good_ext / "README.md").unlink()
    findings = lint_extension(good_ext)
    assert any(f.code == "AEOS010" and "README" in f.message for f in findings)


def test_lint_fails_for_malformed_manifest(good_ext: Path) -> None:
    (good_ext / "axiom-extension.toml").write_text("this = is = not [toml")
    findings = lint_extension(good_ext)
    assert any(f.code == "AEOS020" for f in findings)


def test_lint_fails_for_missing_aeos_version(good_ext: Path) -> None:
    data = tomllib.loads((good_ext / "axiom-extension.toml").read_text())
    data["extension"].pop("aeos_version", None)
    # Rewrite manifest without aeos_version — schema-fail expected too
    _write_toml(good_ext / "axiom-extension.toml", data)
    findings = lint_extension(good_ext)
    # Either the schema check (AEOS021) or the explicit check (AEOS022) fires
    codes = [f.code for f in findings]
    assert "AEOS022" in codes or any(c == "AEOS021" for c in codes)


def test_lint_fails_for_missing_all_declaration(good_ext: Path) -> None:
    (good_ext / "good_ext" / "__init__.py").write_text("# no __all__ here\n")
    findings = lint_extension(good_ext)
    assert any(f.code == "AEOS032" for f in findings)


def test_lint_fails_when_manifest_name_disagrees_with_dir(good_ext: Path) -> None:
    data = tomllib.loads((good_ext / "axiom-extension.toml").read_text())
    data["extension"]["name"] = "different_name"
    _write_toml(good_ext / "axiom-extension.toml", data)
    findings = lint_extension(good_ext)
    assert any(f.code == "AEOS030" for f in findings)


def test_lint_fails_when_pyproject_name_disagrees(good_ext: Path) -> None:
    data = tomllib.loads((good_ext / "pyproject.toml").read_text())
    data["project"]["name"] = "wrong_name"
    _write_toml(good_ext / "pyproject.toml", data)
    findings = lint_extension(good_ext)
    assert any(f.code == "AEOS036" for f in findings)


def test_lint_fails_for_missing_standard_test(good_ext: Path) -> None:
    (good_ext / "tests" / "unit_tests" / "test_standard.py").unlink()
    findings = lint_extension(good_ext)
    assert any(f.code == "AEOS050" for f in findings)


def test_lint_warns_for_missing_capability_dirs(good_ext: Path) -> None:
    # Bronze is capability-dir-warning rather than error; scaffold has all 7
    # so blow one away and check that a warning (not an error) is emitted.
    import shutil

    shutil.rmtree(good_ext / "good_ext" / "tools")
    findings = lint_extension(good_ext)
    assert any(f.code == "AEOS040" and f.severity == "warning" for f in findings)
    # Warnings alone must not fail the command.
    provider = LintProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args([str(good_ext)])
    ctx = CliContext(cwd=good_ext)
    assert provider.run(args, ctx) == 0


def test_lint_points_to_nonexistent_path(tmp_path: Path, run_lint) -> None:
    target = tmp_path / "does_not_exist"
    findings = lint_extension(target)
    assert any(f.code == "AEOS001" for f in findings)
    assert run_lint(target) != 0


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


def test_json_output_is_machine_readable(good_ext: Path, capsys) -> None:
    (good_ext / "CHANGELOG.md").unlink()  # force one error
    provider = LintProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args([str(good_ext), "--json"])
    ctx = CliContext(cwd=good_ext)
    rc = provider.run(args, ctx)
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["error_count"] >= 1
    assert payload["extension"].endswith("good_ext")
    assert all({"code", "severity", "message", "remediation"} <= set(f) for f in payload["findings"])
    assert rc != 0


def test_json_output_clean_for_good_extension(good_ext: Path, capsys) -> None:
    provider = LintProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args([str(good_ext), "--json"])
    ctx = CliContext(cwd=good_ext)
    rc = provider.run(args, ctx)
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["error_count"] == 0
    assert rc == 0


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _write_toml(path: Path, data: dict) -> None:
    """Round-trip-safe TOML writer for tests (uses tomlkit under the hood)."""
    import tomlkit

    path.write_text(tomlkit.dumps(data), encoding="utf-8")
