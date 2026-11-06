# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``next.suggest`` — the one capability the chat home, the slide-out, ``axi next``
and the MCP tool all call."""

from __future__ import annotations

import json
from typing import Any

from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec, default_registry

from .. import engine


def _view(raw: Any) -> dict[str, Any] | None:
    """The view context from any transport: a dict, or JSON text (MCP strings)."""
    if raw in (None, ""):
        return None
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw))
    except ValueError:
        return {"label": str(raw)}
    return parsed if isinstance(parsed, dict) else None


def _principal(params: dict[str, Any]) -> str:
    """Whose history may be read.

    A caller that STATES the principal (the web route, from its session) is
    taken at its word — including an empty one, which means nobody: no
    history is read. Falling back to this machine's operator there would hand
    an anonymous browser the node owner's recent work. Only a caller that
    states nothing (the CLI, MCP on the operator's own machine) is the open
    principal.
    """
    if "_principal" in params:
        return str(params.get("_principal") or "")
    try:
        from axiom.infra.principal import open_principal

        return str(open_principal() or "")
    except Exception:  # noqa: BLE001 - no principal: history is not read, offers still come
        return ""


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    try:
        limit = int(params.get("limit") or engine.MAX_CARDS)
    except (TypeError, ValueError):
        limit = engine.MAX_CARDS
    roles = params.get("_roles") or []
    asker = engine.Asker(
        principal=_principal(params),
        roles=frozenset(str(r) for r in roles),
        view=_view(params.get("view_context") or params.get("view")),
    )
    return SkillResult(value=engine.suggest(asker, limit=limit))


def bind(registry: SkillRegistry) -> list[str]:
    name = "next.suggest"
    if not registry.has(name):
        registry.register_skill(
            SkillSpec(
                name=name,
                fn=run,
                description=(
                    "What the person would plausibly do next: up to five ranked offers from what they "
                    "are looking at, who they are, and their recent work — card 1 the most "
                    "forward-looking question. Each has a kind (prompt or action), a label, and why. "
                    "Read-only; offers only what exists here."
                ),
                inputs={
                    "view_context": "optional: what the person is looking at ({surface, label, focused, …})",
                    "limit": "max offers (default and ceiling 5)",
                },
                side_effects=False,
                idempotent=True,
                risk="read",
                cost_class="lookup",
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
