# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""schedule skills — invocable through the platform SkillRegistry.

Each skill is a plain ``(params, ctx) -> SkillResult`` function. Per
ADR-056: every CLI verb maps 1:1 to a skill-fn here. Agent personas
reach the same surface via the registry.

Namespace: ``schedule`` (the extension's CLI noun).
"""

from __future__ import annotations

from axiom.infra.skills import SkillRegistry, SkillSpec, default_registry

from . import cancel, fire_now, pause, register, resume, status
from . import items as items_mod
from . import list as list_mod

_NAMESPACE = "schedule"

_SKILLS = {
    "register": register.run,
    "pause": pause.run,
    "resume": resume.run,
    "cancel": cancel.run,
    "list": list_mod.run,
    "fire-now": fire_now.run,
    "status": status.run,
}


def bind(registry: SkillRegistry) -> None:
    """Register every schedule skill into ``registry``."""
    for verb, fn in _SKILLS.items():
        name = f"{_NAMESPACE}.{verb}"
        if registry.has(name):
            continue
        registry.register(name, fn)
    # The agenda read (MVP-24): declared, read-only, on every surface.
    for name, fn, description, inputs in items_mod.SPECS:
        if registry.has(name):
            continue
        registry.register_skill(
            SkillSpec(
                name=name,
                fn=fn,
                description=description,
                inputs=inputs,
                side_effects=False,
                idempotent=True,
                surfaces=("cli", "mcp", "agent_tool", "skill_md"),
                risk="read",
            ),
            mutating=False,
        )


def bind_default() -> SkillRegistry:
    registry = default_registry()
    bind(registry)
    return registry


__all__ = ["bind", "bind_default"]
