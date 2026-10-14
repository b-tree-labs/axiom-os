# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The ``features`` capabilities, declared once for the CLI, MCP and agents (ADR-056).

``features.list`` is read-only. ``features.enable``, ``features.disable`` and
``features.role`` change what this node shows; they never install or uninstall
anything, so each is reversible by its opposite.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from axiom.infra import node_functions as nf
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec, default_registry


def _commands() -> dict[str, dict]:
    try:
        from axiom.axiom_cli import _merge_extension_commands

        return _merge_extension_commands()
    except Exception:  # noqa: BLE001
        return {}


def _snapshot(cfg: nf.NodeConfig, commands: dict[str, dict] | None = None) -> dict[str, Any]:
    commands = _commands() if commands is None else commands
    return {
        "role": cfg.role,
        "functions": list(cfg.functions),
        "features": list(cfg.features),
        "confined": cfg.confined,
        "agent_policy": cfg.agent_policy,
        "maintenance": cfg.maintenance_level,
        "record_of_truth": cfg.record_of_truth_in_force,
        "config": str(nf.config_path()),
        "problem": cfg.problem,
        "rows": [asdict(r) for r in nf.feature_rows(cfg, commands)],
        "roles": {
            name: {"functions": list(r.functions), "description": r.description}
            for name, r in nf.roles().items()
        },
    }


def list_(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    return SkillResult(value=_snapshot(nf.load()))


def enable(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    name = str(params.get("name") or "").strip()
    if not name:
        return SkillResult(ok=False, errors=["name a function, command or background-agents to turn on"])
    commands = _commands()
    try:
        cfg = nf.enable(name, known=commands)
    except (nf.UnknownFeature, nf.AgentPolicyRefused) as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(value=_snapshot(cfg, commands), actions_taken=[f"turned on {name}"])


def agents(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Declare the site's agent policy: none, assist or local."""
    policy = str(params.get("policy") or "").strip()
    try:
        cfg = nf.set_agent_policy(policy)
    except nf.UnknownFeature as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(value=_snapshot(cfg), actions_taken=[f"this site's agent policy is now {policy}"])


def maintenance(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Declare how much remote maintenance this site accepts: off, requests or sessions."""
    level = str(params.get("level") or "").strip()
    try:
        cfg = nf.set_maintenance_level(level)
    except nf.UnknownFeature as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(value=_snapshot(cfg), actions_taken=[f"this site's remote maintenance is now {level}"])


def disable(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    name = str(params.get("name") or "").strip()
    try:
        cfg = nf.disable(name)
    except nf.RoleFunction as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(value=_snapshot(cfg), actions_taken=[f"turned off {name}"])


def record_of_truth(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Declare which copy of this site's data wins: upstream or local (ADR-180)."""
    truth = str(params.get("truth") or "").strip()
    try:
        cfg = nf.set_record_of_truth(truth)
    except nf.UnknownFeature as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    meaning = (
        "the host holds this site's shared record (contributor)"
        if truth == "upstream"
        else "this node is the record; the host holds a copy of what is shared (local-first)"
    )
    return SkillResult(
        value=_snapshot(cfg), actions_taken=[f"record of truth is now {truth}: {meaning}"]
    )


def role(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Apply a named role, or ``clear`` to show everything again."""
    name = str(params.get("name") or "").strip()
    settings = dict(params.get("settings") or {})
    if name in ("clear", "none", ""):
        cfg = nf.clear()
        return SkillResult(value=_snapshot(cfg), actions_taken=["cleared the role; every command shows"])
    known = nf.roles()
    if name not in known:
        choices = ", ".join(sorted(known)) or "none are installed"
        return SkillResult(ok=False, errors=[f"no role called {name!r}; roles: {choices}"])
    spec = known[name]
    merged = {**dict(spec.settings), **settings}
    cfg = nf.apply(role=name, functions=spec.functions, settings=merged)
    return SkillResult(
        value=_snapshot(cfg),
        actions_taken=[f"this node's role is now {name} ({', '.join(spec.functions)})"],
    )


_SPECS = (
    SkillSpec(
        name="features.list",
        fn=list_,
        description=(
            "What this node is for (its role and ADR-164 functions), which commands and "
            "background services are on, and what else can be turned on. Read-only."
        ),
        inputs={},
        side_effects=False,
        idempotent=True,
        surfaces=("cli", "mcp", "agent_tool", "skill_md"),
    ),
    SkillSpec(
        name="features.enable",
        fn=enable,
        description="Turn on a node function, a command or background-agents. Reversible; installs nothing.",
        inputs={"name": "str"},
        side_effects=True,
        idempotent=True,
        surfaces=("cli", "mcp", "agent_tool", "skill_md"),
    ),
    SkillSpec(
        name="features.disable",
        fn=disable,
        description="Turn off something turned on with features.enable. A role's own functions are changed with features.role.",
        inputs={"name": "str"},
        side_effects=True,
        idempotent=True,
        surfaces=("cli", "mcp", "agent_tool", "skill_md"),
    ),
    SkillSpec(
        name="features.agents",
        fn=agents,
        description=(
            "Declare this site's agent policy: none (no agent runs and nothing calls a language model), "
            "assist (the same here; maintenance proposals come from elsewhere for approval) or local."
        ),
        inputs={"policy": "str"},
        side_effects=True,
        idempotent=True,
        # A site decision, made by a person at the command line. Never offered to
        # an AI client, which could otherwise widen its own permissions.
        surfaces=("cli", "skill_md"),
    ),
    SkillSpec(
        name="features.maintenance",
        fn=maintenance,
        description=(
            "Declare how much remote maintenance this site accepts: off (nothing remote runs), "
            "requests (signed allowlisted requests; changes wait for approval here) or sessions "
            "(also support sessions a person here opens). off stops it."
        ),
        inputs={"level": "str"},
        side_effects=True,
        idempotent=True,
        # A site decision, made by a person at the command line, like the agent policy.
        surfaces=("cli", "skill_md"),
    ),
    SkillSpec(
        name="features.record_of_truth",
        fn=record_of_truth,
        description=(
            "Declare which copy of this site's data wins (ADR-180): upstream (contributor, "
            "the default) or local (local-first). Reconcile reads it."
        ),
        inputs={"truth": "str"},
        side_effects=True,
        idempotent=True,
        # A site decision, made by a person at the command line, like the agent policy.
        surfaces=("cli", "skill_md"),
    ),
    SkillSpec(
        name="features.role",
        fn=role,
        description="Give this node a named role (a preset of functions), or 'clear' to show everything again.",
        inputs={"name": "str", "settings": "dict?"},
        side_effects=True,
        idempotent=True,
        surfaces=("cli", "mcp", "agent_tool", "skill_md"),
    ),
)


def bind(registry: SkillRegistry) -> list[str]:
    names = []
    for spec in _SPECS:
        if not registry.has(spec.name):
            registry.register_skill(spec, mutating=spec.side_effects)
        names.append(spec.name)
    return names


register = bind


def bind_default() -> SkillRegistry:
    registry = default_registry()
    bind(registry)
    return registry


__all__ = [
    "agents",
    "bind",
    "bind_default",
    "disable",
    "enable",
    "list_",
    "maintenance",
    "record_of_truth",
    "register",
    "role",
]
