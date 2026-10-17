# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Integration-test base classes for AEOS-conformant extensions.

Integration tests exercise the extension against real or realistic
services. They tend to be heavier than unit tests: slower, may touch the
filesystem through ``tmp_axiom_home``, may spin up a ``MockFederation``,
etc.

The base classes here mirror the unit-test hierarchy but add integration-
level hooks: end-to-end smoke tests, cross-capability workflows, and
capability-level observability probes.

Tests inheriting from these classes are automatically marked with the
``integration`` pytest marker declared in the axiom-tests plugin, making
it easy to skip or include them via ``-m integration``.
"""

from axiom_tests.integration_tests.extension import ExtensionIntegrationTests
from axiom_tests.integration_tests.standard import (
    AdapterIntegrationTests,
    AgentIntegrationTests,
    CommandIntegrationTests,
    HookIntegrationTests,
    ServiceIntegrationTests,
    SkillIntegrationTests,
    ToolIntegrationTests,
)

__all__ = [
    "AdapterIntegrationTests",
    "AgentIntegrationTests",
    "CommandIntegrationTests",
    "ExtensionIntegrationTests",
    "HookIntegrationTests",
    "ServiceIntegrationTests",
    "SkillIntegrationTests",
    "ToolIntegrationTests",
]
