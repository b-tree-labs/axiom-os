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

from axiom.infra.skills import SkillRegistry, default_registry

from . import cancel, fire_now, list as list_mod, pause, register, resume, status

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


def bind_default() -> SkillRegistry:
    registry = default_registry()
    bind(registry)
    return registry


__all__ = ["bind", "bind_default"]
