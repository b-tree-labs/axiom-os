# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""serve skills — invocable through the SkillRegistry (ADR-056).

``serve.run`` composes and launches the one HTTP app. The CLI verb
``axi serve`` is a thin wrapper over it (``http/cli.py``).
"""

from __future__ import annotations

from axiom.infra.skills import SkillRegistry, default_registry

from . import serve as _serve

_NAMESPACE = "serve"

_SKILLS = {
    "run": _serve.run,
}


def bind(registry: SkillRegistry) -> None:
    """Register every serve skill into ``registry``."""
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


__all__ = ["bind", "bind_default"]
