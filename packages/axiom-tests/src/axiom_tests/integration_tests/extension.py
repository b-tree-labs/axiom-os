# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Extension-level integration test base class.

Reuses the unit-level ``ExtensionStandardTests`` configuration hooks so
extensions don't have to duplicate their manifest-path fixture. Adds
integration-level end-to-end checks that may import the extension's
package, inspect installed entry points, or exercise ``axiom-extension.toml``
``consumes`` declarations against the surrounding environment.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from axiom_tests.unit_tests.extension import ExtensionStandardTests


@pytest.mark.integration
class ExtensionIntegrationTests(ExtensionStandardTests):
    """Integration-level extension conformance.

    Inherits every unit-level check and adds tests that require the
    extension to be installed / importable in the test environment.
    """

    @property
    def require_importable_package(self) -> bool:
        """Extensions override to declare whether the package must import."""
        return True

    def test_package_is_importable(self, extension_manifest: dict[str, Any]) -> None:
        if not self.require_importable_package:
            pytest.skip("extension does not require an importable package")
        name = extension_manifest["extension"]["name"]
        try:
            importlib.import_module(name)
        except ImportError as exc:  # pragma: no cover - error path
            pytest.fail(
                f"cannot import extension package {name!r}: {exc}. "
                "Ensure the extension is installed (``pip install -e .``) "
                "before running integration tests."
            )

    def test_entry_points_match_manifest(self, extension_manifest: dict[str, Any]) -> None:
        """Every manifest ``provides.entry`` should resolve at runtime."""
        if not self.require_importable_package:
            pytest.skip("package not importable — cannot resolve entry points")
        provides = extension_manifest.get("extension", {}).get("provides", [])
        unresolved: list[str] = []
        for block in provides:
            entry = block.get("entry")
            if not entry or ":" not in entry:
                continue
            module_path, _, attr = entry.partition(":")
            try:
                module = importlib.import_module(module_path)
            except ImportError as exc:
                unresolved.append(f"{entry} — import failed: {exc}")
                continue
            if not hasattr(module, attr):
                unresolved.append(f"{entry} — {attr!r} not on {module_path}")
        assert not unresolved, "unresolved entry declarations:\n  " + "\n  ".join(unresolved)


__all__ = ["ExtensionIntegrationTests"]
