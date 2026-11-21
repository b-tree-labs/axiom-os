# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The install-health skills, declared once: ``doctor.run``, ``binding.status``, ``update.check``, ``update.apply``.

Every surface routes through the registry. Chat offers them to every conversation (they are how a
person gets help with their own install in plain English); MCP and agents get them the same way.
"""

from __future__ import annotations

from axiom.infra.skill_declarations import NextStep
from axiom.infra.skills import SkillRegistry, SkillSpec

from . import binding as _binding
from . import doctor as _doctor
from . import update_apply as _apply
from . import update_check as _check

_SURFACES = ("cli", "mcp", "agent_tool")

SPECS = (
    SkillSpec(
        name="doctor.run",
        fn=_doctor.run,
        description=(
            "Check this install's health: Python, environment, package, command on PATH, model "
            "gateway, background service, site connection. Says what is wrong and the exact fix "
            "for each. Makes no change and calls no model. Answers 'check my setup' or "
            "'something is broken'. Not the harness's own /run."
        ),
        inputs={"error": "optional error text the person saw, echoed back beside the checks"},
        surfaces=_SURFACES,
        side_effects=False,
        idempotent=True,
        risk="read",
        requires=(),
        next_steps=(
            NextStep(
                "node.doctor",
                "ok",
                "the same checks with the node's context: inventory, drift and the fix for each (KEEL, ADR-230)",
            ),
        ),
    ),
    SkillSpec(
        name="binding.status",
        fn=_binding.run,
        description=(
            "Is this install connected to a site node, which site, is the node reachable now, and where "
            "do data, documents and the model come from (or is this test mode on invented data). "
            "Answers 'am I connected to the site?'. Reads only."
        ),
        inputs={"probe": "ask the bound node whether it answers (default true)"},
        surfaces=_SURFACES,
        side_effects=False,
        idempotent=True,
        risk="read",
        requires=(),
        next_steps=(
            NextStep("node.inventory", "ok", "what the machine runs, if this install is the node"),
        ),
    ),
    SkillSpec(
        name="update.check",
        fn=_check.run,
        description=(
            "Is a newer version available, and what would an update change? A dry run: nothing is "
            "installed. Answers 'is there a newer version?'. Reads only."
        ),
        inputs={},
        surfaces=_SURFACES,
        side_effects=False,
        idempotent=True,
        risk="read",
        requires=(),
    ),
    SkillSpec(
        name="update.apply",
        fn=_apply.run,
        description=(
            "Update this install: dependencies, database migrations and agent registration, after a "
            "state snapshot. CHANGES THE INSTALL, so a conversation asks the person first. Run "
            "update.check beforehand and tell the person what it found."
        ),
        inputs={},
        surfaces=_SURFACES,
        side_effects=True,
        idempotent=True,
        risk="reversible-write",
        requires=(),
    ),
)


def bind(registry: SkillRegistry) -> None:
    for spec in SPECS:
        if not registry.has(spec.name):
            registry.register_skill(spec, mutating=spec.side_effects is True)


def bind_default() -> SkillRegistry:
    from axiom.infra.skills import default_registry

    registry = default_registry()
    bind(registry)
    return registry


__all__ = ["SPECS", "bind", "bind_default"]
