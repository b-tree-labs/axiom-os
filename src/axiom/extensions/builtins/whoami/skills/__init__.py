# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The ``whoami`` capability, declared once and projected to the CLI, MCP and agents.

Read-only, so an agent may call it freely: at the start of a task to learn
where it is, or when a result surprises it, before guessing.
"""

from __future__ import annotations

import json
from typing import Any

from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec, default_registry

from .. import sections


def _as_list(raw: Any) -> list[str]:
    """``sections`` as a list, whatever transport delivered it.

    The CLI hands over a list. MCP schemas type the parameter as a string, so
    a client sends ``"you"``, ``"you,access"`` or ``'["you","access"]'`` — and
    iterating a string yields characters, which is how ``"you"`` became the
    unknown sections ``['y', 'o', 'u']``.
    """
    if raw is None or raw == "":
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    text = str(raw).strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except ValueError:
            pass
    return [w for w in (t.strip() for t in text.replace(",", " ").split()) if w]


def _as_bool(raw: Any) -> bool:
    """``all`` as a bool. A string ``"false"`` is truthy in Python; it must not be here."""
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    wanted = _as_list(params.get("sections"))
    if _as_bool(params.get("all")):
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
