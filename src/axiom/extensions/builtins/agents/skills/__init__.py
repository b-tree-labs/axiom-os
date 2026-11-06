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
from .chains import chain_priority, chain_stop, chains
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
    for spec in _CHAIN_SPECS:
        if not registry.has(spec.name):
            registry.register_skill(spec, mutating=spec.side_effects is True)


_CHAIN_SPECS = (
    SkillSpec(
        name="agents.chain_priority",
        fn=chain_priority,
        description=(
            "Set a chain's priority: critical, high, normal or background. When the node is short of "
            "room, higher-priority chains keep writing and lower ones wait their turn (the strategy is "
            "the chains.pressure_strategy setting). Answers 'let this one finish first'."
        ),
        inputs={
            "id": "the chain id (agents.chains lists them)",
            "priority": "critical | high | normal | background",
        },
        surfaces=("cli", "mcp", "agent_tool"),
        side_effects=True,
        idempotent=True,
        risk="reversible-write",
        requires=(),
    ),
    SkillSpec(
        name="agents.chains",
        fn=chains,
        description=(
            "The chains of hand-offs in flight: each request that set agents handing work to each "
            "other, how long it has run, its deadline, how many hops, the last skill called and its "
            "status. Answers 'what are the agents doing', 'is anything looping'. Reads only."
        ),
        inputs={"all": "also list chains that ended in the last day"},
        surfaces=("cli", "mcp", "agent_tool"),
        side_effects=False,
        idempotent=True,
        risk="read",
        requires=(),
    ),
    SkillSpec(
        name="agents.chain_stop",
        fn=chain_stop,
        description=(
            "Stop a chain of hand-offs: its next hand-off is refused, so it ends between steps, "
            "never in the middle of one. Answers 'stop that loop'. The stop and who asked are recorded."
        ),
        inputs={"id": "the chain id (agents.chains lists them)", "reason": "why, in a few words"},
        surfaces=("cli", "mcp", "agent_tool"),
        side_effects=True,
        idempotent=True,
        risk="reversible-write",
        requires=(),
    ),
)


def bind_default() -> SkillRegistry:
    reg = default_registry()
    bind(reg)
    return reg


def verbs() -> list[str]:
    return list(_SKILLS)


__all__ = [
    "address",
    "ask",
    "bind",
    "bind_default",
    "chain_priority",
    "chain_stop",
    "chains",
    "roster",
    "verbs",
]
