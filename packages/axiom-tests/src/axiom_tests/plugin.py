# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Pytest plugin registration for axiom-tests.

Registered via the ``pytest11`` entry point in ``pyproject.toml``:

    [project.entry-points.pytest11]
    axiom_tests = "axiom_tests.plugin"

When ``axiom-tests`` is installed in a test environment, pytest automatically
discovers this plugin and makes the fixtures defined in
``axiom_tests.fixtures`` available to every test — no ``conftest.py`` import
required on the consumer side.
"""

from __future__ import annotations

import pytest

# Fixture modules imported via ``pytest_plugins`` below — pytest will load
# them as if the consumer had listed them in their own conftest.
pytest_plugins: tuple[str, ...] = (
    "axiom_tests.fixtures.llm",
    "axiom_tests.fixtures.federation",
    "axiom_tests.fixtures.oidc",
    "axiom_tests.fixtures.registry",
    "axiom_tests.fixtures.home",
    "axiom_tests.fixtures.manifest",
    "axiom_tests.fixtures.strategies",
    "axiom_tests.fixtures.hooks",
)


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers contributed by axiom-tests."""
    config.addinivalue_line(
        "markers",
        "aeos_capability(kind): mark a test as exercising an AEOS capability kind",
    )
    config.addinivalue_line(
        "markers",
        "aeos_conformance(level): mark a test as a conformance check at bronze/silver/gold",
    )


__all__ = ["pytest_configure", "pytest_plugins"]
