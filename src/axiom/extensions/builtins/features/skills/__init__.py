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
    except nf.UnknownFeature as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(value=_snapshot(cfg, commands), actions_taken=[f"turned on {name}"])


def disable(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    name = str(params.get("name") or "").strip()
    try:
        cfg = nf.disable(name)
    except nf.RoleFunction as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(value=_snapshot(cfg), actions_taken=[f"turned off {name}"])


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


__all__ = ["bind", "bind_default", "disable", "enable", "list_", "register", "role"]
