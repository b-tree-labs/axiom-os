# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Smoke tests for the integration-test base classes.

We verify that each integration class inherits the unit-level tests
and carries the ``integration`` marker. Actual integration behavior is
exercised by extensions that subclass these in their own test suites.
"""

from __future__ import annotations

import inspect

import pytest

from axiom_tests.integration_tests import (
    AdapterIntegrationTests,
    AgentIntegrationTests,
    CommandIntegrationTests,
    ExtensionIntegrationTests,
    HookIntegrationTests,
    ServiceIntegrationTests,
    SkillIntegrationTests,
    ToolIntegrationTests,
)
from axiom_tests.unit_tests import (
    AdapterTests,
    AgentTests,
    CommandTests,
    ExtensionStandardTests,
    HookTests,
    ServiceTests,
    SkillTests,
    ToolTests,
)

BASE_PAIRS = [
    (ExtensionIntegrationTests, ExtensionStandardTests),
    (AgentIntegrationTests, AgentTests),
    (ToolIntegrationTests, ToolTests),
    (CommandIntegrationTests, CommandTests),
    (ServiceIntegrationTests, ServiceTests),
    (AdapterIntegrationTests, AdapterTests),
    (SkillIntegrationTests, SkillTests),
    (HookIntegrationTests, HookTests),
]


@pytest.mark.parametrize(
    "integ, unit", BASE_PAIRS, ids=lambda c: c.__name__ if inspect.isclass(c) else str(c)
)
def test_integration_class_inherits_unit(integ: type, unit: type) -> None:
    assert issubclass(integ, unit), f"{integ.__name__} should inherit from {unit.__name__}"


@pytest.mark.parametrize(
    "integ, _unit", BASE_PAIRS, ids=lambda c: c.__name__ if inspect.isclass(c) else str(c)
)
def test_integration_class_has_integration_marker(integ: type, _unit: type) -> None:
    # pytest attaches markers via ``pytestmark`` class attribute.
    markers = getattr(integ, "pytestmark", [])
    names = {m.name for m in markers}
    assert "integration" in names, f"{integ.__name__} is missing the ``integration`` marker"
