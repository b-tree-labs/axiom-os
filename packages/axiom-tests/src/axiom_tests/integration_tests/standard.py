# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Integration-level counterparts to each unit-test capability base class.

Each subclass inherits the full unit-level test suite and adds the
``integration`` marker. Integration-specific tests can be added on top; the
design intent is that extensions may compose unit + integration conformance
by inheriting from both, or just from the integration variant.
"""

from __future__ import annotations

import pytest

from axiom_tests.unit_tests.adapter import AdapterTests
from axiom_tests.unit_tests.agent import AgentTests
from axiom_tests.unit_tests.cmd import CommandTests
from axiom_tests.unit_tests.hook import HookTests
from axiom_tests.unit_tests.service import ServiceTests
from axiom_tests.unit_tests.skill import SkillTests
from axiom_tests.unit_tests.tool import ToolTests


@pytest.mark.integration
class AgentIntegrationTests(AgentTests):
    """Integration-level agent conformance; inherits all unit-level checks."""


@pytest.mark.integration
class ToolIntegrationTests(ToolTests):
    """Integration-level tool conformance."""

    def test_invoke_is_runnable(self, tool_class: type) -> None:
        """Tool instantiation + invoke() signature does not crash.

        Extensions override to pass realistic inputs. Default behavior is
        just to ensure the shape is sane so that CI can catch import-time
        breakage.
        """
        if not hasattr(tool_class, "invoke"):
            pytest.skip("tool has no invoke method (covered by unit tests)")
        # We can't call it safely without knowing the input schema;
        # integration extensions subclass and do this for real.
        pytest.skip("override test_invoke_is_runnable in your subclass with real input")


@pytest.mark.integration
class CommandIntegrationTests(CommandTests):
    """Integration-level cmd conformance."""


@pytest.mark.integration
class ServiceIntegrationTests(ServiceTests):
    """Integration-level service conformance."""

    def test_lifecycle_does_not_crash(self, service_class: type) -> None:
        """Smoke test for start/stop without asserting side effects."""
        pytest.skip(
            "override test_lifecycle_does_not_crash in your subclass to drive the actual service"
        )


@pytest.mark.integration
class AdapterIntegrationTests(AdapterTests):
    """Integration-level adapter conformance."""


@pytest.mark.integration
class SkillIntegrationTests(SkillTests):
    """Integration-level skill conformance."""


@pytest.mark.integration
class HookIntegrationTests(HookTests):
    """Integration-level hook conformance."""


__all__ = [
    "AdapterIntegrationTests",
    "AgentIntegrationTests",
    "CommandIntegrationTests",
    "HookIntegrationTests",
    "ServiceIntegrationTests",
    "SkillIntegrationTests",
    "ToolIntegrationTests",
]
