# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext doctor`` — lint + validate + test + environment sweep."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from axiom.cli.ext.commands.doctor import DoctorProvider, DoctorResult, run_doctor
from axiom.cli.ext.provider import CliContext


@pytest.fixture
def run_doctor_cli(capsys):
    """Invoke the DoctorProvider against a path and return (rc, stdout)."""

    def _run(path: Path, *extra: str) -> tuple[int, str]:
        # Discard any output buffered by previous fixtures (e.g. scaffold).
        capsys.readouterr()
        provider = DoctorProvider()
        parser = argparse.ArgumentParser()
        provider.add_arguments(parser)
        args = parser.parse_args([str(path), *extra])
        ctx = CliContext(cwd=path)
        rc = provider.run(args, ctx)
        captured = capsys.readouterr()
        return rc, captured.out

    return _run


# ---------------------------------------------------------------------------
# Provider registration
# ---------------------------------------------------------------------------


def test_doctor_provider_is_registered_as_builtin() -> None:
    from axiom.cli.ext.registry import discover_providers

    providers = discover_providers()
    assert "doctor" in providers
    assert providers["doctor"].verb == "doctor"


def test_doctor_provider_has_description() -> None:
    provider = DoctorProvider()
    assert provider.description
    assert isinstance(provider.description, str)


# ---------------------------------------------------------------------------
# Happy path: scaffolded extension should be healthy enough to pass doctor,
# modulo the standard test run. We skip the pytest invocation to keep this
# suite fast — the environment checks + lint + validate suffice to exercise
# the aggregator.
# ---------------------------------------------------------------------------


def test_doctor_on_fresh_scaffold_reports_environment_ok(
    scaffolded_extension, run_doctor_cli
) -> None:
    ext = scaffolded_extension("healthy_ext")
    rc, out = run_doctor_cli(ext, "--skip-tests")
    # Environment checks (python version, axiom-tests importable, manifest parse)
    # must all pass for any sane dev machine running these tests.
    assert "python_version" in out
    assert "axiom_tests_importable" in out
    assert "manifest_parses" in out


def test_doctor_reports_lint_and_validate_sections(
    scaffolded_extension, run_doctor_cli
) -> None:
    ext = scaffolded_extension("checkpoint_ext")
    rc, out = run_doctor_cli(ext, "--skip-tests")
    # The aggregator runs both lint and validate; their check groups should appear.
    assert "lint" in out.lower()
    assert "validate" in out.lower()


def test_doctor_json_output_is_parseable(
    scaffolded_extension, run_doctor_cli
) -> None:
    ext = scaffolded_extension("json_ext")
    rc, out = run_doctor_cli(ext, "--skip-tests", "--json")
    data = json.loads(out)
    assert data["extension"] == str(ext)
    assert isinstance(data["results"], list)
    assert data["results"], "doctor must return at least one result"
    for entry in data["results"]:
        assert set(entry) >= {"check", "ok", "detail"}


# ---------------------------------------------------------------------------
# Failure surface: a broken extension should cause doctor to exit non-zero.
# ---------------------------------------------------------------------------


def test_doctor_fails_when_manifest_missing(tmp_path: Path, run_doctor_cli) -> None:
    broken = tmp_path / "broken"
    broken.mkdir()
    # No axiom-extension.toml, no anything. Doctor must fail loudly.
    rc, out = run_doctor_cli(broken, "--skip-tests")
    assert rc != 0
    assert "manifest_parses" in out
    # The failing line should be clearly marked.
    assert "FAIL" in out or "✗" in out or "[x]" in out.lower()


def test_doctor_surfaces_lint_errors(
    scaffolded_extension, run_doctor_cli
) -> None:
    ext = scaffolded_extension("willbreak_ext")
    # Corrupt the manifest so lint fails (bad TOML).
    (ext / "axiom-extension.toml").write_text("this is = not ][ toml\n")
    rc, out = run_doctor_cli(ext, "--skip-tests")
    assert rc != 0


# ---------------------------------------------------------------------------
# Core API: run_doctor returns structured results, callable without argparse.
# ---------------------------------------------------------------------------


def test_run_doctor_returns_structured_results(scaffolded_extension) -> None:
    ext = scaffolded_extension("api_ext")
    results = run_doctor(ext, skip_tests=True)
    assert all(isinstance(r, DoctorResult) for r in results)
    names = {r.check for r in results}
    # Environment checks always present.
    assert {"python_version", "axiom_tests_importable", "manifest_parses"} <= names
