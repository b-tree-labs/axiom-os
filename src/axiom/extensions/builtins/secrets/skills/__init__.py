# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``secrets`` skills — invocable through the SkillRegistry.

Per ADR-056, every ``axi secrets <verb>`` CLI surface is a thin wrapper
over a skill function with shape::

    def run(params: dict, ctx: SkillContext) -> SkillResult

Skills live under the ``secrets`` namespace (the CLI noun).
"""

from __future__ import annotations

from axiom.infra.skills import SkillRegistry, default_registry

from . import diagnose

_NAMESPACE = "secrets"

_SKILLS = {
    "diagnose": diagnose.run,
}


def bind(registry: SkillRegistry) -> None:
    """Register every secrets skill into ``registry``. Idempotent."""
    for verb, fn in _SKILLS.items():
        name = f"{_NAMESPACE}.{verb}"
        if registry.has(name):
            continue
        registry.register(name, fn)


def bind_default() -> SkillRegistry:
    """Bind into the process-local default registry; idempotent."""
    reg = default_registry()
    bind(reg)
    return reg


def verbs() -> list[str]:
    """Return the verb names (no namespace prefix). Used by the CLI parser."""
    return list(_SKILLS)


__all__ = ["bind", "bind_default", "verbs"]
