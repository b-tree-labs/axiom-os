# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi connector`` skills — ADR-056 thin wrappers."""

from __future__ import annotations

from axiom.infra.skills import SkillRegistry, default_registry

from . import add, reconnect, status

_NAMESPACE = "connector"

_SKILLS = {
    "add": add.run,
    "status": status.run,
    "reconnect": reconnect.run,
}


def bind(registry: SkillRegistry) -> None:
    for verb, fn in _SKILLS.items():
        name = f"{_NAMESPACE}.{verb}"
        if registry.has(name):
            continue
        registry.register(name, fn)


def bind_default() -> SkillRegistry:
    reg = default_registry()
    bind(reg)
    return reg


__all__ = ["bind", "bind_default"]
