# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Fleet skills — thin, registry-bound entry points (ADR-056)."""

from __future__ import annotations

from axiom.infra.skill_declarations import NextStep
from axiom.infra.skills import SkillRegistry, SkillSpec

from . import report, status

SPECS = (
    SkillSpec(
        name="fleet.status",
        fn=status.run,
        description="The fleet console's view: which nodes report, when each last did, and what drifted between them. Reads only.",
        surfaces=("cli", "mcp", "agent_tool"),
        side_effects=False,
        idempotent=True,
        risk="read",
        requires=(),
    ),
    SkillSpec(
        name="fleet.report",
        fn=report.run,
        description="Node-side push: gather this node's snapshot and POST it out to the fleet console (ADR-119: the console never reaches in).",
        surfaces=("cli", "mcp", "agent_tool"),
        side_effects=True,
        idempotent=True,
        risk="reversible-write",
        requires=(),
        next_steps=(NextStep("node.report", "error", "read the node document locally when the push fails"),),
    ),
)


def bind(registry: SkillRegistry) -> None:
    for spec in SPECS:
        if not registry.has(spec.name):
            registry.register_skill(spec, mutating=spec.side_effects is True)


def bind_default() -> SkillRegistry:
    registry = SkillRegistry()
    bind(registry)
    return registry


__all__ = ["SPECS", "bind", "bind_default", "report", "status"]
