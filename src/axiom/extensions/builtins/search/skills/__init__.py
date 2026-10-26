# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``search.query`` — the one capability every surface calls (ADR-056)."""

from __future__ import annotations

import json
from typing import Any

from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec, default_registry

from .. import engine


def _as_list(raw: Any) -> list[str]:
    """``kinds`` from any transport: a list, CSV, space-separated or a JSON list."""
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
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


def _scope(params: dict[str, Any], ctx: SkillContext) -> engine.Scope:
    """The asker's bound. A caller (the HTTP route) may pass the principal and
    sites it resolved; otherwise the node's own scope and the open principal."""
    from axiom.infra import site_scope

    principal = str(params.get("_principal") or "")
    if not principal:
        try:
            from axiom.infra.principal import open_principal

            principal = str(open_principal() or "")
        except Exception:  # noqa: BLE001 - no principal narrows nothing; scope still bounds sites
            principal = ""
    sites = params.get("_sites")
    if sites is None:
        sc = site_scope.resolve()
        sites = None if sc.unbounded else frozenset(sc.sites)
    return engine.Scope(
        principal=principal,
        sites=frozenset(sites) if sites is not None else None,
        include_archived=_as_bool(params.get("include_archived")),
    )


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    query = str(params.get("query") or params.get("q") or "").strip()
    if not query:
        return SkillResult(ok=False, errors=["say what to search for: query is required"])
    mode = str(params.get("mode") or engine.FULL)
    if mode not in (engine.FULL, engine.SUGGEST):
        return SkillResult(ok=False, errors=[f"mode is {engine.FULL!r} or {engine.SUGGEST!r}"])
    kinds = _as_list(params.get("kinds"))
    unknown = [k for k in kinds if k not in engine.KINDS]
    if unknown:
        return SkillResult(
            ok=False, errors=[f"unknown kind(s) {unknown}; choose from {list(engine.KINDS)}"]
        )
    try:
        limit = int(params.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    return SkillResult(
        value=engine.search(query, _scope(params, ctx), mode=mode, kinds=kinds or None, limit=limit)
    )


def bind(registry: SkillRegistry) -> list[str]:
    name = "search.query"
    if not registry.has(name):
        registry.register_skill(
            SkillSpec(
                name=name,
                fn=run,
                description=(
                    "Search what this node holds — conversations (titles and message text), signed "
                    "logbook records, and the document corpus — bounded by what the asker may read. "
                    "Returns ranked rows {kind, id, title, snippet, url, score, site}, and `skipped` "
                    "naming any source not available here. Read-only."
                ),
                inputs={
                    "query": "text to find (required)",
                    "kinds": f"optional list from {list(engine.KINDS)}",
                    "mode": "'full' (default) or 'suggest' (fast, palette-sized)",
                    "limit": "max rows (default 20, max 50)",
                    "include_archived": "bool: include archived conversations",
                },
                side_effects=False,
                idempotent=True,
                risk="read",
                # Each call scans stores (chat, logbooks, the corpus).
                cost_class="scan",
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
