# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""graduation skills — invocable through the SkillRegistry (ADR-056).

``graduation.status`` reports the shadow outcome log. The CLI verb
``axi graduation status`` is a thin wrapper over it (``graduation/cli.py``).
"""

from __future__ import annotations

from axiom.infra.skills import SkillRegistry, default_registry

from . import status as _status

_NAMESPACE = "graduation"

_SKILLS = {
    "status": (_status.run, {"mutating": False}),
}


def bind(registry: SkillRegistry) -> None:
    """Register every graduation skill into ``registry``."""
    for verb, (fn, opts) in _SKILLS.items():
        name = f"{_NAMESPACE}.{verb}"
        if registry.has(name):
            continue
        registry.register(name, fn, **opts)


def bind_default() -> SkillRegistry:
    """Bind into the process-local default registry; idempotent."""
    reg = default_registry()
    bind(reg)
    return reg


__all__ = ["bind", "bind_default"]
