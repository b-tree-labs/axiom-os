# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Agent-invocable skills for the `agents` noun.

Bound into the SkillRegistry so they dispatch through `invoke_capability`
rather than being called directly. That chokepoint is where the authority
gate, the action audit and the capability telemetry live — and a DELEGATION
surface is the last thing that should sit outside it. "Which agent was asked
to do what, by whom" is exactly the question an audit trail exists to answer.

`SkillSpec` with `surfaces=("cli", "mcp", "agent_tool")` is what makes these
reachable from a consuming harness; without it they would be CLI-only and the
roster would stay as unreachable as it was before.
"""

from __future__ import annotations

from axiom.infra.skills import SkillRegistry, SkillSpec, default_registry

from .address import address
from .ask import ask
from .roster import roster

_NAMESPACE = "agents"
_SKILLS = {"address": address, "ask": ask, "roster": roster}


def bind(registry: SkillRegistry) -> None:
    for verb, fn in _SKILLS.items():
        name = f"{_NAMESPACE}.{verb}"
        if registry.has(name):
            continue
        registry.register_skill(
            SkillSpec(
                name=name,
                fn=fn,
                # `roster` only reads the manifests. `ask` runs a turn as
                # another agent, which is neither idempotent nor free of
                # effects — saying otherwise would let a caller retry a
                # delegation believing it costs nothing.
                idempotent=(verb == "roster"),
                side_effects=(verb in {"ask", "address"}),
                surfaces=("cli", "mcp", "agent_tool"),
            )
        )


def bind_default() -> SkillRegistry:
    reg = default_registry()
    bind(reg)
    return reg


def verbs() -> list[str]:
    return list(_SKILLS)


__all__ = ["address", "ask", "bind", "bind_default", "roster", "verbs"]
