# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The ``flags.*`` skills: every feature-flag verb (ADR-210, ADR-254 amended 2026-11-12).

The implementation lives in ``axiom.extensions.builtins.features.skills`` beside
the ``features.*`` node verbs, because both nouns share one registry; this package
binds the same specs, so ``axiom_flags__*`` answers whichever extension binds
first. Binding here is idempotent with binding there.

The flag lifecycle verbs (LOOP-F11), ``flags.stale`` and ``flags.stale_notify``, are this
package's own and bind here only.
"""

from __future__ import annotations

from axiom.extensions.builtins.features.skills import (
    CLEAR,
    default_account,
    list_flags,
    resolve_flags,
    set_flag,
    this_node,
)
from axiom.extensions.builtins.features.skills import bind as _bind_flag_verbs
from axiom.infra.skills import SkillRegistry, SkillSpec, default_registry

from .stale import STALE_READERS, stale, stale_notify

_LIFECYCLE = (
    SkillSpec(
        name="flags.stale",
        fn=stale,
        description=(
            "Flags that are work to remove (LOOP-F11): fully on past their review date, or off with no "
            "reference left in code, grouped by owner; and the flags that declare no owner. For site admins "
            "(read-only) and the node's maintainers."
        ),
        inputs={"environment": "optional environment (default: this one)", "today": "optional YYYY-MM-DD",
                "roots": "optional list of directories the reference search reads"},
        side_effects=False,
        idempotent=True,
        surfaces=("cli", "mcp", "agent_tool", "skill_md"),
        risk="read",
        requires=(STALE_READERS,),
    ),
    SkillSpec(
        name="flags.stale_notify",
        fn=stale_notify,
        description=(
            "Send each configured maintainer (setting flags.maintainers) one grouped notice of the stale flags "
            "(the weekly schedule runs it); never to a site admin. Does nothing while flag_review_notices is "
            "off or no maintainer is configured."
        ),
        inputs={"environment": "optional", "today": "optional YYYY-MM-DD", "roots": "optional list"},
        side_effects=True,
        surfaces=("cli", "skill_md"),
        risk="reversible-write",
        requires=(),
    ),
)


def bind(registry: SkillRegistry) -> list[str]:
    names = _bind_flag_verbs(registry)
    for spec in _LIFECYCLE:
        if not registry.has(spec.name):
            registry.register_skill(spec, mutating=bool(spec.side_effects))
        names.append(spec.name)
    return names


def bind_default() -> SkillRegistry:
    registry = default_registry()
    bind(registry)
    return registry


register = bind

__all__ = [
    "CLEAR",
    "bind",
    "bind_default",
    "default_account",
    "list_flags",
    "register",
    "resolve_flags",
    "set_flag",
    "stale",
    "stale_notify",
    "this_node",
]
