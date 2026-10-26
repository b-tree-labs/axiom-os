# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``ExtensionStandardTests`` — extension-level conformance base class.

Subclasses provide a single fixture, ``extension_manifest_path``, pointing
at ``axiom-extension.toml``. The base class then verifies:

- Manifest exists and parses
- Manifest validates against the AEOS JSON Schema
- All required files per AEOS §5.2 are present
- ``pyproject.toml`` and manifest agree on name / version
- ``__init__.py`` of the package declares ``__all__``
- ``aeos_version`` is declared

Extensions override ``required_files`` or ``required_docs`` to tighten or
loosen the checks; defaults reflect AEOS 0.1.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from axiom_tests._manifest import (
    build_validator,
    load_manifest,
    validate_manifest,
)

if TYPE_CHECKING:
    from collections.abc import Iterable


REQUIRED_FILES: tuple[str, ...] = (
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "pyproject.toml",
    "axiom-extension.toml",
)

# Relaxed file set for built-ins that ship inside a host package
# (e.g. axi-platform's extensions/builtins/). README / CHANGELOG / LICENSE
# / pyproject.toml belong to the host package, not each built-in.
BUILTIN_REQUIRED_FILES: tuple[str, ...] = (
    "axiom-extension.toml",
)


class ExtensionStandardTests:
    """Extension-level conformance checks (AEOS §5.2, §5.3, §7)."""

    # ---- Overridable fixtures -------------------------------------------

    @pytest.fixture
    def extension_manifest_path(self) -> Path:
        """Return the absolute path to ``axiom-extension.toml``.

        Subclasses MUST override this fixture to point at their manifest.
        """
        raise NotImplementedError(
            "subclasses of ExtensionStandardTests must override "
            "extension_manifest_path to return the path to axiom-extension.toml"
        )

    @pytest.fixture
    def extension_root(self, extension_manifest_path: Path) -> Path:
        """Directory containing the extension (where the manifest lives)."""
        return extension_manifest_path.parent

    @pytest.fixture
    def extension_manifest(self, extension_manifest_path: Path) -> dict[str, Any]:
        """Parsed contents of ``axiom-extension.toml``."""
        return load_manifest(extension_manifest_path)

    @pytest.fixture
    def pyproject(self, extension_root: Path) -> dict[str, Any] | None:
        """Parsed contents of ``pyproject.toml`` sitting next to the manifest.

        Returns ``None`` for built-ins that don't ship their own pyproject.
        Agreement tests below skip when this fixture is ``None``.
        """
        path = extension_root / "pyproject.toml"
        if not path.exists():
            return None
        with path.open("rb") as fh:
            return tomllib.load(fh)

    @pytest.fixture
    def is_builtin(self, extension_manifest: dict[str, Any]) -> bool:
        """Built-ins ship inside a host package and use the flat layout."""
        return bool(extension_manifest.get("extension", {}).get("builtin", False))

    # ---- Overridable knobs ----------------------------------------------

    @property
    def required_files(self) -> Iterable[str]:
        """File names required at the extension root (AEOS §5.2).

        Overridden below via :func:`effective_required_files` to relax to
        :data:`BUILTIN_REQUIRED_FILES` when the manifest declares
        ``builtin = true``. Subclasses that want stricter enforcement can
        still override ``required_files`` directly.
        """
        return REQUIRED_FILES

    def effective_required_files(self, is_builtin: bool) -> Iterable[str]:
        if is_builtin and self.required_files is REQUIRED_FILES:
            return BUILTIN_REQUIRED_FILES
        return self.required_files

    @property
    def require_agents_md(self) -> bool:
        """AGENTS.md is optional by default (AEOS §5.3)."""
        return False

    @property
    def require_docs_dir(self) -> bool:
        """``docs/`` is required for extensions with >= 2 capability kinds."""
        return False

    # ---- Standard tests -------------------------------------------------

    def test_manifest_exists(self, extension_manifest_path: Path) -> None:
        assert extension_manifest_path.exists(), (
            f"AEOS manifest not found at {extension_manifest_path}"
        )

    def test_manifest_parses(self, extension_manifest: dict[str, Any]) -> None:
        assert "extension" in extension_manifest, "manifest must have an [extension] section"

    def test_manifest_validates_against_schema(self, extension_manifest: dict[str, Any]) -> None:
        errors = validate_manifest(extension_manifest, validator=build_validator())
        assert not errors, "AEOS manifest schema errors:\n  " + "\n  ".join(errors)

    def test_required_files_present(
        self, extension_root: Path, is_builtin: bool
    ) -> None:
        required = self.effective_required_files(is_builtin)
        missing = [name for name in required if not (extension_root / name).exists()]
        assert not missing, f"extension {extension_root.name} is missing required files: {missing}"

    def test_aeos_version_declared(self, extension_manifest: dict[str, Any]) -> None:
        ext = extension_manifest.get("extension", {})
        assert ext.get("aeos_version"), (
            "manifest [extension] must declare aeos_version per AEOS §6.2"
        )

    def test_manifest_and_pyproject_agree_on_name(
        self, extension_manifest: dict[str, Any], pyproject: dict[str, Any] | None
    ) -> None:
        if pyproject is None:
            pytest.skip("no pyproject.toml (built-in ships inside host package)")
        manifest_name = extension_manifest["extension"]["name"]
        py_name = pyproject.get("project", {}).get("name")
        # Allow the pyproject to use hyphens where the manifest uses
        # underscores (valid-per-PEP-503 normalization). Also permit a
        # common convention of prefixing with the host package: e.g.
        # manifest "diagnostics" ↔ pyproject "axiom-diagnostics".
        assert py_name is not None, "pyproject.toml missing [project].name"
        norm_py = py_name.replace("-", "_")
        norm_m = manifest_name.replace("-", "_")
        matches = (
            norm_py == norm_m
            or norm_py.endswith("_" + norm_m)
        )
        assert matches, (
            f"pyproject name {py_name!r} and manifest name {manifest_name!r} disagree"
        )

    def test_manifest_and_pyproject_agree_on_version(
        self, extension_manifest: dict[str, Any], pyproject: dict[str, Any] | None
    ) -> None:
        if pyproject is None:
            pytest.skip("no pyproject.toml (built-in ships inside host package)")
        mv = extension_manifest["extension"]["version"]
        pv = pyproject.get("project", {}).get("version")
        assert pv is not None, "pyproject.toml missing [project].version"
        assert pv == mv, f"pyproject version {pv!r} does not equal manifest version {mv!r}"

    def test_public_api_declared(
        self, extension_root: Path, extension_manifest: dict[str, Any]
    ) -> None:
        """The package __init__.py must declare ``__all__`` (AEOS §7.3).

        Accepted layouts:
          * Compound (standalone): ``<root>/<pkg>/__init__.py``
          * Compound with src/:   ``<root>/src/<pkg>/__init__.py``
          * Flat (built-in):       ``<root>/__init__.py`` when the
            root directory name matches the manifest package name
        """
        pkg_name = extension_manifest["extension"]["name"]
        init_candidates = [
            extension_root / pkg_name / "__init__.py",
            extension_root / "src" / pkg_name / "__init__.py",
        ]
        if extension_root.name == pkg_name:
            init_candidates.append(extension_root / "__init__.py")
        init = next((p for p in init_candidates if p.exists()), None)
        assert init is not None, (
            f"could not find {pkg_name}/__init__.py under {extension_root} "
            f"(also checked src/ and flat-builtin layouts)"
        )
        text = init.read_text(encoding="utf-8")
        # For flat-builtin layouts, __all__ is optional — the host
        # package's __init__.py typically covers the public API.
        if init == extension_root / "__init__.py":
            return
        assert "__all__" in text, f"{init} must declare __all__ per AEOS §7.3"

    def test_agents_md_present_if_required(self, extension_root: Path) -> None:
        if not self.require_agents_md:
            pytest.skip("extension does not require AGENTS.md")
        assert (extension_root / "AGENTS.md").exists(), (
            "AGENTS.md required by this extension's configuration"
        )

    def test_docs_dir_present_if_required(self, extension_root: Path) -> None:
        if not self.require_docs_dir:
            pytest.skip("extension does not require docs/")
        assert (extension_root / "docs").is_dir(), (
            "docs/ directory required by this extension's configuration"
        )

    def test_has_at_least_one_provides_block(self, extension_manifest: dict[str, Any]) -> None:
        provides = extension_manifest.get("extension", {}).get("provides", [])
        assert provides, "AEOS §6.2 requires at least one [[extension.provides]] block"


__all__ = ["ExtensionStandardTests", "REQUIRED_FILES"]
