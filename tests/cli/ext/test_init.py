# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``axi ext init`` — AEOS-conformant scaffolding."""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

import pytest

from axiom.cli.ext.commands.init import InitProvider, validate_name
from axiom.cli.ext.provider import CliContext


@pytest.fixture
def run_init(tmp_path: Path):
    """Run ``axi ext init <name>`` targeting a tmpdir; return the ext path."""

    def _run(name: str, **kwargs: str) -> Path:
        provider = InitProvider()
        parser = argparse.ArgumentParser()
        provider.add_arguments(parser)
        argv = [name, "--dir", str(tmp_path)]
        for key, value in kwargs.items():
            argv.extend([f"--{key.replace('_', '-')}", value])
        args = parser.parse_args(argv)
        ctx = CliContext(cwd=tmp_path)
        rc = provider.run(args, ctx)
        assert rc == 0, f"init returned {rc}"
        return tmp_path / name

    return _run


# ---------------------------------------------------------------------------
# Name validation
# ---------------------------------------------------------------------------


def test_validate_name_accepts_purpose_name() -> None:
    assert validate_name("classroom") is None
    assert validate_name("syllabus_extraction") is None
    assert validate_name("reactor_physics") is None


@pytest.mark.parametrize(
    "bad,fragment",
    [
        ("my_agent", "type suffix"),
        ("classroom_tool", "type suffix"),
        ("memory_cmd", "type suffix"),
        ("canvas_adapter", "type suffix"),
        ("foo-bar", "underscore"),
        ("Classroom", "lowercase"),
        ("1classroom", "lowercase"),
        ("axiom", "reserved"),
        ("extension", "reserved"),
        ("", "empty"),
    ],
)
def test_validate_name_rejects_bad_names(bad: str, fragment: str) -> None:
    err = validate_name(bad)
    assert err is not None
    assert fragment in err.lower()


# ---------------------------------------------------------------------------
# Scaffold layout
# ---------------------------------------------------------------------------


def test_init_creates_canonical_layout(run_init) -> None:
    ext = run_init("test_ext")
    # Package directory + empty marker files
    pkg = ext / "test_ext"
    assert (pkg / "__init__.py").exists()
    assert (pkg / "py.typed").exists()
    for sub in ("agents", "tools", "commands", "services", "adapters", "skills", "hooks"):
        assert (pkg / sub).is_dir()
        # .gitkeep so empty dirs survive git
        assert (pkg / sub / ".gitkeep").exists()
    # Internal private package
    assert (pkg / "_internal" / "__init__.py").exists()


def test_init_creates_tests_layout(run_init) -> None:
    ext = run_init("test_ext")
    assert (ext / "tests" / "unit_tests" / "test_standard.py").exists()
    assert (ext / "tests" / "integration_tests").is_dir()
    assert (ext / "tests" / "fixtures").is_dir()
    assert (ext / "tests" / "conftest.py").exists()


def test_init_creates_docs_layout(run_init) -> None:
    ext = run_init("test_ext")
    for sub in ("prds", "specs", "decisions", "working", "reference"):
        assert (ext / "docs" / sub).is_dir()
    assert (ext / "docs" / "overview.md").exists()


def test_init_creates_required_top_level_files(run_init) -> None:
    ext = run_init("test_ext")
    assert (ext / "README.md").exists()
    assert (ext / "CHANGELOG.md").exists()
    assert (ext / "LICENSE").exists()
    assert (ext / "AGENTS.md").exists()
    assert (ext / "pyproject.toml").exists()
    assert (ext / "axiom-extension.toml").exists()


# ---------------------------------------------------------------------------
# File contents
# ---------------------------------------------------------------------------


def test_init_standard_test_inherits_from_axiom_tests(run_init) -> None:
    ext = run_init("test_ext")
    content = (ext / "tests" / "unit_tests" / "test_standard.py").read_text()
    assert "from axiom_tests.unit_tests import" in content
    assert "ExtensionStandardTests" in content
    assert "extension_manifest_path" in content


def test_init_package_init_declares_empty_all(run_init) -> None:
    ext = run_init("test_ext")
    init_py = (ext / "test_ext" / "__init__.py").read_text()
    # Must declare __all__ — empty list is valid for a fresh scaffold
    assert "__all__" in init_py


def test_init_changelog_is_keep_a_changelog(run_init) -> None:
    ext = run_init("test_ext")
    text = (ext / "CHANGELOG.md").read_text()
    assert "Keep a Changelog" in text
    assert "[Unreleased]" in text


def test_init_license_is_apache(run_init) -> None:
    ext = run_init("test_ext")
    text = (ext / "LICENSE").read_text()
    assert "Apache License" in text
    assert "Version 2.0" in text


def test_init_manifest_has_required_fields(run_init) -> None:
    ext = run_init("test_ext")
    data = tomllib.loads((ext / "axiom-extension.toml").read_text())
    assert data["extension"]["name"] == "test_ext"
    assert data["extension"]["version"] == "0.1.0"
    assert data["extension"]["aeos_version"] == "0.1.0"
    assert data["extension"]["license"] == "Apache-2.0"
    assert data["extension"]["description"]
    # §6.2 requires ≥1 [[extension.provides]] block
    assert data["extension"]["provides"]


def test_init_manifest_provides_block_declares_progressive_disclosure(run_init) -> None:
    # Regression: without explicit tier + intent_groups, every freshly-
    # scaffolded extension gets DEFAULT_CMD_TIER="core" + empty intent_groups,
    # which silently hides its placeholder command from every basic-role user
    # at default competency. The scaffold must emit explicit values so a new
    # author sees their extension immediately AND learns the disclosure
    # vocabulary from the comment block. See axiom/cli/help_engine.py.
    ext = run_init("test_ext")
    data = tomllib.loads((ext / "axiom-extension.toml").read_text())
    provides = data["extension"]["provides"]
    assert provides, "scaffold must emit at least one provides block"
    placeholder = provides[0]
    assert placeholder.get("tier") == "starter", (
        f"placeholder cmd must declare tier='starter' for day-one "
        f"visibility; got {placeholder.get('tier')!r}"
    )
    assert placeholder.get("intent_groups") == ["start"], (
        f"placeholder cmd must declare intent_groups=['start'] so every "
        f"role can see it during early development; got "
        f"{placeholder.get('intent_groups')!r}"
    )


def test_init_manifest_respects_owner_and_license_flags(run_init) -> None:
    ext = run_init("test_ext", owner="ut-austin", license="MIT")
    data = tomllib.loads((ext / "axiom-extension.toml").read_text())
    assert data["extension"]["owner"] == "ut-austin"
    assert data["extension"]["license"] == "MIT"


def test_init_pyproject_has_name_and_version(run_init) -> None:
    ext = run_init("test_ext")
    data = tomllib.loads((ext / "pyproject.toml").read_text())
    assert data["project"]["name"] == "test_ext"
    assert data["project"]["version"] == "0.1.0"


def test_init_copyright_header_present(run_init) -> None:
    ext = run_init("test_ext")
    for path in (
        ext / "test_ext" / "__init__.py",
        ext / "test_ext" / "_internal" / "__init__.py",
        ext / "tests" / "unit_tests" / "test_standard.py",
        ext / "tests" / "conftest.py",
    ):
        text = path.read_text()
        assert "Copyright (c) 2026 The University of Texas at Austin" in text, path


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------


def test_init_rejects_existing_directory(tmp_path: Path) -> None:
    (tmp_path / "exists").mkdir()
    provider = InitProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args(["exists", "--dir", str(tmp_path)])
    ctx = CliContext(cwd=tmp_path)
    rc = provider.run(args, ctx)
    assert rc != 0


def test_init_rejects_bad_name_via_cli(tmp_path: Path) -> None:
    provider = InitProvider()
    parser = argparse.ArgumentParser()
    provider.add_arguments(parser)
    args = parser.parse_args(["my_agent", "--dir", str(tmp_path)])
    ctx = CliContext(cwd=tmp_path)
    rc = provider.run(args, ctx)
    assert rc != 0
    assert not (tmp_path / "my_agent").exists()


# ---------------------------------------------------------------------------
# Scaffold must be Bronze-conformant from day one
# ---------------------------------------------------------------------------


def test_scaffolded_manifest_passes_aeos_schema(run_init) -> None:
    """The manifest we emit must validate against the AEOS 0.1 schema."""
    ext = run_init("test_ext")
    from axiom_tests import load_manifest, validate_manifest

    manifest = load_manifest(ext / "axiom-extension.toml")
    errors = validate_manifest(manifest)
    assert not errors, f"scaffold manifest failed AEOS schema: {errors}"
