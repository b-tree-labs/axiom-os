# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``authz`` skills — invocable through the SkillRegistry.

Per ADR-056, every ``axi audit <verb>`` CLI surface is a thin wrapper
over a skill function with the shape::

    def run(params: dict, ctx: SkillContext) -> SkillResult

Skills live under the ``audit`` namespace (the CLI noun) so an agent
persona can invoke ``audit.list`` without going through argparse.

AUTHZ-1 ships ``audit.list`` + ``audit.show``. ``chain``/``causes``/
``graduation``/``explain`` follow in AUTHZ-2 / AUTHZ-3.
"""

from __future__ import annotations

from axiom.infra.skills import SkillRegistry, default_registry

from . import causes_verdicts as _causes_mod
from . import chain_verdicts as _chain_mod
from . import explain as _explain_mod
from . import graduation as _graduation_mod
from . import healthcheck as _healthcheck_mod
from . import lint as _lint_mod
from . import list_verdicts as _list_mod
from . import show_verdict as _show_mod

_NAMESPACE = "audit"

_SKILLS = {
    "list": _list_mod.run,
    "show": _show_mod.run,
    "chain": _chain_mod.run,
    "causes": _causes_mod.run,
    "graduation": _graduation_mod.run,
    "explain": _explain_mod.run,
    "lint": _lint_mod.run,
    "healthcheck": _healthcheck_mod.run,
}


def bind(registry: SkillRegistry) -> None:
    """Register every authz audit skill into ``registry``. Idempotent."""
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
