# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Skills for the `process` noun, bound into the SkillRegistry (ADR-056)."""

from __future__ import annotations

from axiom.infra.process_containment import paused_agents
from axiom.infra.skill_declarations import NextStep, Precondition, Ref
from axiom.infra.skills import SkillRegistry, SkillSpec, default_registry

from ._common import registry_for
from .ls import ls
from .panic import panic
from .resume import resume
from .show import show
from .stop import stop
from .sweep import sweep

_NAMESPACE = "process"


def _registered(params, ctx) -> tuple[bool, str]:
    """The id names exactly one record in the registry. Reads the record files; touches no process."""
    ident = str((params or {}).get("id") or "").strip()
    if not ident:
        return False, "no process id was given"
    try:
        found = registry_for(ctx).get(ident) is not None
    except Exception:  # noqa: BLE001 - an unreadable registry is an unmet precondition, not a crash
        return False, "the process registry could not be read"
    return found, "" if found else f"no unique registered process matches {ident!r}"


_PROCESS_REGISTERED = Precondition(
    "process-registered",
    "the target must be a process the harness registered",
    check=_registered,
    consumes="process_id",
    param="id",
    fix="list what is registered with 'axi process ls' and pass one of its ids as id",
)


def _paused(params, ctx) -> tuple[bool, str]:
    agent = str((params or {}).get("agent") or "").strip()
    if not agent:
        return False, "no agent was named"
    try:
        found = agent in paused_agents(registry_for(ctx).state_dir)
    except Exception:  # noqa: BLE001
        return False, "the containment record could not be read"
    return found, "" if found else f"{agent!r} was not paused by containment"


_AGENT_PAUSED = Precondition(
    "agent-paused",
    "the agent must have been paused by containment",
    check=_paused,
    fix="see which agents are on with 'axi agents status'; only an agent that 'axi process panic --kill' paused can be resumed",
)

_PROCESS_ID = Ref("process_id", "pass as id to process.show or process.stop")

_SPECS = {
    "ls": dict(
        description="List the processes the harness started and what is wrong with any of them. "
                    "match narrows by name, kind, agent, extension or command; exactly one match returns its process_id.",
        inputs={"match": "text found in a process's id, kind, agent, extension, purpose or command"},
        risk="read", requires=(),
        produces=(_PROCESS_ID,),
        next_steps=(
            NextStep("process.show", "produced:process_id", "read its full record and findings before touching it",
                     feeds={"id": "process_id"}),
            NextStep("process.stop", "produced:process_id", "stop it (opens a plan; nothing stops until a person approves)",
                     feeds={"id": "process_id"}),
            NextStep("process.sweep", "ok", "look for rogue processes and runaway agents across everything"),
        ),
        recipes=("incident-response",),
    ),
    "show": dict(
        description="One registered process: its record, its live state and its findings.",
        inputs={"id": "process id, or a unique prefix"},
        risk="read", requires=(_PROCESS_REGISTERED,),
        next_steps=(
            NextStep("process.stop", "ok", "stop it (opens a plan; nothing stops until a person approves)"),
        ),
    ),
    "stop": dict(
        description="Stop one registered process: SIGTERM, a grace period, then SIGKILL. Dry run unless kill.",
        inputs={"id": "process id, or a unique prefix", "kill": "really stop it", "grace": "seconds to wait before SIGKILL",
                "why": "reason recorded in the audit", "actor": "who is asking"},
        # A stopped process does not come back with its in-memory state. Classified by the kill form.
        risk="destructive", requires=(_PROCESS_REGISTERED,),
        produces=(Ref("process_id", "pass as id to process.show to confirm it is gone"),),
        next_steps=(
            NextStep("process.ls", "ok", "confirm it is gone and see what else is registered"),
            NextStep("process.sweep", "error", "see why the record does not verify"),
        ),
        recipes=("incident-response",),
    ),
    "sweep": dict(
        description="Find rogue processes and runaway agents. Reports by default; auto acts on the policy class; kill acts on every killable finding.",
        inputs={"auto": "act on the policy class only", "kill": "stop every killable finding"},
        # The report form changes nothing, the kill form stops processes. Classified by the kill form.
        risk="destructive", requires=(),
        next_steps=(NextStep("process.ls", "ok", "confirm what is registered now"),),
    ),
    "panic": dict(
        description="Emergency stop: pause every agent and stop the processes agents own. Dry run unless kill.",
        inputs={"kill": "really do it"},
        risk="destructive", requires=(),
        next_steps=(
            NextStep("process.ls", "ok", "confirm what is left"),
            NextStep("process.resume", "ok", "turn one agent back on when it is safe"),
        ),
        recipes=("credential-leak-response",),
    ),
    "resume": dict(
        description="Undo a containment pause for one agent. Dry run unless yes.",
        inputs={"agent": "the agent to turn back on", "yes": "really do it"},
        risk="reversible-write", requires=(_AGENT_PAUSED,),
        next_steps=(NextStep("process.sweep", "ok", "check that it behaves"),),
    ),
}
_FUNCTIONS = {"ls": ls, "show": show, "stop": stop, "sweep": sweep, "panic": panic, "resume": resume}
_SKILLS = _FUNCTIONS
_READ_ONLY = {"ls", "show"}


def bind(registry: SkillRegistry) -> None:
    for verb, fn in _FUNCTIONS.items():
        name = f"{_NAMESPACE}.{verb}"
        if registry.has(name):
            continue
        registry.register_skill(
            SkillSpec(
                name=name,
                fn=fn,
                idempotent=verb in _READ_ONLY,
                side_effects=verb not in _READ_ONLY,
                surfaces=("cli",),
                **_SPECS[verb],
            ),
            mutating=verb not in _READ_ONLY,
        )


def bind_default() -> SkillRegistry:
    reg = default_registry()
    bind(reg)
    return reg


def verbs() -> list[str]:
    return list(_SKILLS)


__all__ = ["bind", "bind_default", "verbs"]
