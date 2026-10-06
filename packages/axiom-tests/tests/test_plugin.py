# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for the pytest11 plugin registration and marker setup."""

from __future__ import annotations

from importlib import metadata


def test_pytest11_entry_point_registered() -> None:
    eps = metadata.entry_points().select(group="pytest11")
    names = {ep.name for ep in eps}
    assert "axiom_tests" in names, f"axiom-tests pytest11 entry point missing; got {names}"


def test_plugin_module_imports() -> None:
    from axiom_tests import plugin

    assert hasattr(plugin, "pytest_configure")
    assert hasattr(plugin, "pytest_plugins")
    assert "axiom_tests.fixtures.llm" in plugin.pytest_plugins


def test_markers_registered(pytestconfig) -> None:  # type: ignore[no-untyped-def]
    marker_defs = pytestconfig.getini("markers")
    assert any("aeos_capability" in m for m in marker_defs)
    assert any("aeos_conformance" in m for m in marker_defs)


def test_fixtures_available_via_plugin(mock_llm, mock_federation, mock_oidc, mock_registry) -> None:  # type: ignore[no-untyped-def]
    """Smoke test: fixtures resolve without a conftest.py importing them."""
    assert mock_llm is not None
    assert mock_federation is not None
    assert mock_oidc is not None
    assert mock_registry is not None
