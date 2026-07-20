# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""observability skills — invocable through the platform SkillRegistry.

Naming: skills are namespaced under ``observe`` (the extension's CLI
noun). So ``observe.install``, ``observe.verify``, ``observe.diagnose``.
"""

from __future__ import annotations

from axiom.infra.skills import SkillRegistry, default_registry

from . import diagnose, install, verify

_NAMESPACE = "observe"

_SKILLS = {
    "install": install.run,
    "verify": verify.run,
    "diagnose": diagnose.run,
}


def bind(registry: SkillRegistry) -> None:
    """Register every observability skill into ``registry``."""
    for verb, fn in _SKILLS.items():
        name = f"{_NAMESPACE}.{verb}"
        if registry.has(name):
            continue
        registry.register(name, fn)


def bind_default() -> SkillRegistry:
    reg = default_registry()
    bind(reg)
    return reg


def verbs() -> list[str]:
    return list(_SKILLS)


__all__ = ["bind", "bind_default", "verbs"]
