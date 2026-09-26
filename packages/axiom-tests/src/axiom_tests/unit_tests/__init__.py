# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Unit-test base classes for AEOS-conformant extensions.

Each capability kind (agent, tool, cmd, service, adapter, skill, hook) has a
corresponding ``*Tests`` base class. Extension authors subclass these in
their own ``tests/unit_tests/test_standard.py``, overriding capability
properties (default ``False``) to declare what the extension supports.

In addition, ``ExtensionStandardTests`` verifies the extension-level
conformance requirements from AEOS §5.2 and §5.3.

See ``docs/specs/spec-aeos-0.1.md §8`` for the specification that drives
this file.
"""

from axiom_tests.unit_tests.adapter import AdapterTests
from axiom_tests.unit_tests.agent import AgentTests
from axiom_tests.unit_tests.cmd import CommandTests
from axiom_tests.unit_tests.extension import ExtensionStandardTests
from axiom_tests.unit_tests.hook import HookTests
from axiom_tests.unit_tests.service import ServiceTests
from axiom_tests.unit_tests.skill import SkillTests
from axiom_tests.unit_tests.tool import ToolTests

__all__ = [
    "AdapterTests",
    "AgentTests",
    "CommandTests",
    "ExtensionStandardTests",
    "HookTests",
    "ServiceTests",
    "SkillTests",
    "ToolTests",
]
