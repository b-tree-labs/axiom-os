# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The noun's own skills, declared once: ``instrument.list`` and ``instrument.status`` (ADR-056).

Both are reads over every registered kind, on the command line, MCP and agents.
A kind's verbs are the kind's skills, named for its own extension (``video.add``;
ADR-256 D5), and are not declared here. ``axi skills emit-md --ext instrument``
turns these specs into each skill's SKILL.md and the manifest's skill blocks.
"""

from __future__ import annotations

from axiom.infra.skill_declarations import NextStep
from axiom.infra.skills import SkillRegistry, SkillSpec, default_registry

from . import listing as _listing
from . import status as _status

_READS = ("cli", "mcp", "agent_tool")

SPECS = (
    SkillSpec(
        name="instrument.list",
        fn=_listing.run,
        description=(
            "List what this node takes in from its sensors: one row per declared instrument, of every "
            "registered instrument kind, with its kind, name, health (ready, degraded, failed, unmounted) "
            "and state. With kinds=true, the registered instrument kinds instead: each one's name, what it "
            "yields (frames, readings), where it came from and what it is, and any kind that was refused, "
            "declared twice or cannot load, with the reason and the fix. A kind that does not answer is "
            "listed under failed; the others still answer. Reads only."
        ),
        inputs={"kinds": "bool"},
        surfaces=_READS,
        side_effects=False,
        idempotent=True,
        open_world=False,
        risk="read",
        requires=(),
        next_steps=(NextStep("instrument.status", "always", "see each kind's summary"),),
    ),
    SkillSpec(
        name="instrument.status",
        fn=_status.run,
        description=(
            "One summary per instrument kind on this node: whether it answered, a one-line summary, its "
            "instruments counted by health, and any error with its fix. A kind that was refused, declared "
            "twice, cannot load or does not answer is shown as not ok with why; the others still answer. "
            "Answers 'is everything this node measures working'. Reads only."
        ),
        inputs={},
        surfaces=_READS,
        side_effects=False,
        idempotent=True,
        open_world=False,
        risk="read",
        requires=(),
        next_steps=(NextStep("instrument.list", "always", "see each instrument and its health"),),
    ),
)


def bind(registry: SkillRegistry) -> list[str]:
    names = []
    for spec in SPECS:
        if not registry.has(spec.name):
            registry.register_skill(spec, mutating=spec.side_effects is True)
        names.append(spec.name)
    return names


def bind_default() -> SkillRegistry:
    """Bind into the process-default registry (what ``axi skills emit-md`` and MCP read)."""
    registry = default_registry()
    bind(registry)
    return registry


__all__ = ["SPECS", "bind", "bind_default"]
