# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""data_platform skills — invocable through the platform SkillRegistry.

Each skill is a plain Python function::

    def run(params: dict, ctx: SkillContext) -> SkillResult: ...

Registration into the default registry happens at first-use via
:func:`bind_default`. Tests build a clean ``SkillRegistry()`` directly
and call :func:`bind` against it; the CLI module's main() calls
:func:`bind_default` once before dispatching.

Naming: skills are namespaced under ``data`` (the extension's CLI
noun). So ``data.install``, ``data.diagnose``, ``data.ingest``, etc.

A skill maps to its CLI verb 1:1 — that's the principle ADR-056
locks in. ``axi data install`` → ``data.install``; same skill called
from any agent persona.
"""

from __future__ import annotations

from axiom.infra.skills import SkillRegistry, default_registry

from . import (
    diagnose,
    ingest,
    install,
    preflight,
    register,
    troubleshoot,
    unregister,
)
from . import (
    list_connectors as _list_mod,
)

_NAMESPACE = "data"

_SKILLS = {
    "install": install.run,
    "diagnose": diagnose.run,
    "ingest": ingest.run,
    "register": register.run,
    "unregister": unregister.run,
    "list": _list_mod.run,
    "troubleshoot": troubleshoot.run,
    "preflight": preflight.run,
}


def bind(registry: SkillRegistry) -> None:
    """Register every data_platform skill into ``registry``."""
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
