# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Software proposes and a person signs (ADR-142). This guard keeps it so.

Chat and MCP reach skills through the `agent_tool` and `mcp` surfaces, so the whole safety
argument is that no skill able to sign is on either surface. This is true today, so these
are normal tests, not expected failures. A unit that adds a signing skill adds its name to
`SIGNING_SKILLS`. A unit that adds a skill which only proposes keeps it in a module that
never calls `service.sign`.
"""

from __future__ import annotations

import ast
import inspect

from axiom.extensions.builtins.attest.skills import SPECS
from axiom.infra.orchestrator import skills as approval_skills

AGENT_SURFACES = {"mcp", "agent_tool"}

#: Skills that sign, or that decide for a person. The later ones arrive with L2 to L5.
SIGNING_SKILLS = {
    "attest.new",
    "attest.sign",
    "attest.supplement",
    "attest.ack",
    "attest.cosign",
    "attest.seal",
    "approval.approve",
    "approval.reject",
}


class _Collect:
    """Stands in for a registry, to read the approval skills' declarations."""

    def __init__(self):
        self.specs = []

    def register_skill(self, spec, **_):
        self.specs.append(spec)


_approval = _Collect()
approval_skills.register_all(_approval)
ALL_SKILLS = (*SPECS, *_approval.specs)


def _calls_service_sign(module) -> bool:
    for node in ast.walk(ast.parse(inspect.getsource(module))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            base = node.func.value
            if node.func.attr == "sign" and isinstance(base, ast.Name) and base.id == "service":
                return True
    return False


def test_no_signing_skill_is_on_an_agent_surface():
    exposed = [s.name for s in ALL_SKILLS if s.name in SIGNING_SKILLS and AGENT_SURFACES & set(s.surfaces or ())]
    assert exposed == []


def test_the_guard_is_not_empty():
    """A guard over a list that matches nothing is green for the wrong reason."""
    present = {s.name for s in ALL_SKILLS} & SIGNING_SKILLS
    assert {"attest.new", "attest.sign"} <= present


def test_a_skill_that_reaches_the_signature_is_on_neither_surface():
    """Checked from the code, so a signing skill with a new name is caught too."""
    sign_module = inspect.getmodule(next(s.fn for s in SPECS if s.name == "attest.sign"))
    assert _calls_service_sign(sign_module)
    for spec in SPECS:
        if AGENT_SURFACES & set(spec.surfaces or ()):
            assert not _calls_service_sign(inspect.getmodule(spec.fn)), spec.name
