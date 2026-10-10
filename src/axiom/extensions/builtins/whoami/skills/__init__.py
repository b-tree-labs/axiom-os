# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The ``whoami`` capability, declared once and projected to the CLI, MCP and agents.

Read-only, so an agent may call it freely: at the start of a task to learn
where it is, or when a result surprises it, before guessing.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec, default_registry

from .. import sections


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    wanted = params.get("sections") or []
    if params.get("all"):
        names = sections.ALL
    elif wanted:
        unknown = [w for w in wanted if w not in sections.BUILTIN]
        if unknown:
            return SkillResult(
                ok=False,
                errors=[f"unknown section(s) {unknown}; choose from {list(sections.BUILTIN)}"],
            )
        names = tuple(wanted)
    else:
        names = sections.DEFAULT
    return SkillResult(value=sections.collect(names))


def bind(registry: SkillRegistry) -> list[str]:
    name = "whoami.report"
    if not registry.has(name):
        registry.register_skill(
            SkillSpec(
                name=name,
                fn=run,
                description=(
                    "Who you are on this machine and what it is: the acting principal and the identity "
                    "behind it, the software running and from where, the node, its site and federation, "
                    "credentials and sign-in (names only), dev nodes, agent harnesses, and the mismatches "
                    "between them. Read-only; call it to reorient before a task or when a result surprises you."
                ),
                inputs={
                    "sections": f"optional list from {list(sections.BUILTIN)}",
                    "all": "bool: every section, including the slower llm probe",
                },
                side_effects=False,
                idempotent=True,
                surfaces=("cli", "mcp", "agent_tool", "skill_md"),
            ),
            mutating=False,
        )
    return [name]


register = bind


def bind_default() -> SkillRegistry:
    registry = default_registry()
    bind(registry)
    return registry


__all__ = ["bind", "bind_default", "register", "run"]
