# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext templates`` — the template registry and listing verb."""

from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path

import pytest

from axiom.cli.ext.commands.init import InitProvider
from axiom.cli.ext.commands.templates import TemplatesProvider
from axiom.cli.ext.provider import CliContext
from axiom.cli.ext.templates import Template, registry

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_includes_compound_template() -> None:
    templates = {t.id: t for t in registry()}
    assert "compound" in templates, "the default compound layout must be registered"
    compound = templates["compound"]
    assert isinstance(compound, Template)
    assert compound.description
    # The compound template is the one `axi ext init` uses by default.
    assert compound.is_default is True


def test_registry_only_one_default() -> None:
    defaults = [t for t in registry() if t.is_default]
    assert len(defaults) == 1, "exactly one template may be marked default"


def test_template_ids_are_unique() -> None:
    ids = [t.id for t in registry()]
    assert len(ids) == len(set(ids)), f"duplicate template ids: {ids}"


def test_template_ids_are_kebab_or_snake_identifiers() -> None:
    import re

    ident = re.compile(r"^[a-z][a-z0-9_\-]*$")
    for t in registry():
        assert ident.match(t.id), f"template id {t.id!r} is not a valid identifier"


# ---------------------------------------------------------------------------
# TemplatesProvider
# ---------------------------------------------------------------------------


@pytest.fixture
def run_templates(capsys):
    def _run(*argv: str) -> tuple[int, str]:
        provider = TemplatesProvider()
        parser = argparse.ArgumentParser()
        provider.add_arguments(parser)
        args = parser.parse_args(list(argv))
        ctx = CliContext(cwd=Path.cwd())
        rc = provider.run(args, ctx)
        captured = capsys.readouterr()
        return rc, captured.out

    return _run


def test_templates_lists_registered_templates(run_templates) -> None:
    rc, out = run_templates()
    assert rc == 0
    # Every registered template should appear by id in the default output.
    for t in registry():
        assert t.id in out, f"{t.id} missing from default output"


def test_templates_default_output_marks_default(run_templates) -> None:
    rc, out = run_templates()
    assert rc == 0
    # Some visual indicator that one template is the default — we do not
    # lock down the exact glyph, but "default" should show up near the default.
    assert "default" in out.lower()


def test_templates_json_output_is_parseable(run_templates) -> None:
    rc, out = run_templates("--json")
    assert rc == 0
    data = json.loads(out)
    assert isinstance(data, list)
    ids = {entry["id"] for entry in data}
    expected = {t.id for t in registry()}
    assert ids == expected
    for entry in data:
        assert set(entry) >= {"id", "description", "is_default"}


# ---------------------------------------------------------------------------
# Integration with `axi ext init --template`
# ---------------------------------------------------------------------------


def test_init_defaults_to_compound_template(tmp_path: Path) -> None:
    provider = InitProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args(["auto_default", "--dir", str(tmp_path)])
    ctx = CliContext(cwd=tmp_path)
    rc = provider.run(args, ctx)
    assert rc == 0
    # Compound layout sentinels
    pkg = tmp_path / "auto_default" / "auto_default"
    for sub in ("agents", "tools", "commands", "services", "adapters", "skills", "hooks"):
        assert (pkg / sub).is_dir(), f"compound template must emit {sub}/"


def test_init_rejects_unknown_template(tmp_path: Path) -> None:
    provider = InitProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args(
        ["nope_ext", "--dir", str(tmp_path), "--template", "not-a-real-template"]
    )
    ctx = CliContext(cwd=tmp_path)
    rc = provider.run(args, ctx)
    assert rc != 0
    assert not (tmp_path / "nope_ext").exists()


def test_init_accepts_compound_template_explicitly(tmp_path: Path) -> None:
    provider = InitProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args(
        ["explicit_ext", "--dir", str(tmp_path), "--template", "compound"]
    )
    ctx = CliContext(cwd=tmp_path)
    rc = provider.run(args, ctx)
    assert rc == 0
    assert (tmp_path / "explicit_ext" / "axiom-extension.toml").exists()


# ---------------------------------------------------------------------------
# Dispatcher wiring
# ---------------------------------------------------------------------------


def test_templates_provider_is_registered_as_builtin() -> None:
    """The Provider dispatcher must expose ``templates`` as a built-in verb."""
    from axiom.cli.ext.registry import discover_providers

    providers = discover_providers()
    assert "templates" in providers
    assert providers["templates"].verb == "templates"


# ---------------------------------------------------------------------------
# Scaffold still passes AEOS lint from day one — regardless of template
# ---------------------------------------------------------------------------


def test_compound_scaffold_passes_bronze_lint(tmp_path: Path) -> None:
    provider = InitProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args(
        ["bronze_smoke", "--dir", str(tmp_path), "--template", "compound"]
    )
    ctx = CliContext(cwd=tmp_path)
    rc = provider.run(args, ctx)
    assert rc == 0

    from axiom.cli.ext.commands.lint import LintProvider

    lint = LintProvider()
    lint_parser = argparse.ArgumentParser()
    lint.add_arguments(lint_parser)
    lint_args = lint_parser.parse_args([str(tmp_path / "bronze_smoke")])
    lint_ctx = CliContext(cwd=tmp_path, extension_path=tmp_path / "bronze_smoke")
    assert lint.run(lint_args, lint_ctx) == 0


def test_scaffolded_manifest_still_declares_required_fields(tmp_path: Path) -> None:
    provider = InitProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args(["declared_ext", "--dir", str(tmp_path)])
    ctx = CliContext(cwd=tmp_path)
    assert provider.run(args, ctx) == 0

    data = tomllib.loads((tmp_path / "declared_ext" / "axiom-extension.toml").read_text())
    assert data["extension"]["name"] == "declared_ext"
    assert data["extension"]["aeos_version"] == "0.1.0"
    assert data["extension"]["provides"]
