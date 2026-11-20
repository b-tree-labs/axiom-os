# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``chat.history``: a person's own chat sessions, newest first, on every surface (MVP-05-A1).

One skill behind the History page (``GET /api/v1/chat/history``), ``axi chat
history`` and the ``axiom_chat__history`` MCP tool, so the three return one
list. It is the chat API's conversation list, not a second store: the same
``DatabaseSessionStore.list_meta`` with the same ownership clause, the same
``site_scope`` bound and the same row shape (``api._conv``).

Two bounds:

- **Whose.** The asker's own sessions under the store's one ownership rule. A
  request that names nobody is refused rather than answered with every
  session: on the web an empty principal means "not signed in".
- **Which variant.** An isolated variant (``active_variant``) answers as a
  guest of its tenant, as chat does (``HeadlessChat.new_scope``), so it lists
  only that tenant's sessions. A plain or in-place install never lists the
  sessions an isolated variant's tenant holds (VAR-A3).

``q`` matches a title (``matched: "name"``) or any message (``"contents"``,
through the store's ``search_conversations``), and only narrows the list
above: a match outside it does not appear.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult, SkillSpec

NAME = "chat.history"
MAX = 200


def _principal(params: dict[str, Any], ctx: SkillContext | None) -> str:
    # The HTTP route always passes ``_principal`` (possibly empty: not signed
    # in), and its answer is final there. Elsewhere the context's principal.
    if "_principal" in params:
        return str(params.get("_principal") or "").strip()
    handle = getattr(getattr(ctx, "principal", None), "handle", "") if ctx is not None else ""
    if not handle:
        from axiom.infra.principal import local_handle

        handle = local_handle()
    return str(handle or "").strip()


def variant_bound() -> tuple[str | None, frozenset[str], str | None]:
    """``(tenant to keep, tenants to exclude, variant name)`` for the running variant."""
    from axiom.infra.variants import active_variant

    v = active_variant()
    if v is not None and v.tenant:
        return v.tenant, frozenset(), v.name
    try:
        from axiom.infra.variants import load_variants

        variants = load_variants()
    except Exception:  # noqa: BLE001 - an unreadable record keeps the built-in variants
        from axiom.infra.variants import DEFAULTS as variants
    others = frozenset(x.tenant for x in variants.values() if x.tenant)
    return None, others, (v.name if v is not None else None)


def _as_bool(raw: Any) -> bool:
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on", "include"}
    return bool(raw)


def history(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    from axiom.infra import site_scope

    from ..api import _conv, _served_site, _store

    principal = _principal(params, ctx)
    if not principal:
        return SkillResult(ok=False, errors=["sign in to see your chat history"])
    try:
        limit = max(1, min(int(params.get("limit") or 50), MAX))
    except (TypeError, ValueError):
        limit = 50
    archived = _as_bool(params.get("include_archived") or params.get("archived"))
    q = " ".join(str(params.get("q") or params.get("query") or "").split())
    keep, exclude, variant = variant_bound()
    scope = site_scope.resolve()
    store = _store()
    metas = [
        m
        for m in store.list_meta(
            site_id=_served_site(scope),
            tenant_id=keep,
            principal=principal,
            include_archived=archived,
        )
        if scope.permits(m["site_id"]) and (m["tenant_id"] or "") not in exclude
    ]
    hits: dict[str, dict[str, Any]] = {}
    if q:
        for r in store.search_conversations(
            q,
            site_id=_served_site(scope),
            principal=principal,
            include_archived=archived,
            limit=100,
        ):
            hits[r["id"]] = r
    rows: list[dict[str, Any]] = []
    for m in metas:
        row = _conv(m)
        if q:
            if q.lower() in (m["title"] or "").lower():
                row.update(matched="name", snippet="")
            elif m["id"] in hits:
                row.update(matched="contents", snippet=hits[m["id"]].get("snippet") or "")
            else:
                continue
        rows.append(row)
        if len(rows) >= limit:
            break
    return SkillResult(
        ok=True,
        value={"sessions": rows, "count": len(rows), "query": q, "variant": variant},
    )


SPEC = SkillSpec(
    name=NAME,
    fn=history,
    description=(
        "The asker's own Axiom chat sessions, newest first, in the running variant, optionally "
        "filtered by q (title or message text); each row says whether q matched its name or its "
        "contents. The same list the History page shows. Not the harness's own /resume, which "
        "lists the harness's sessions. Reads only."
    ),
    inputs={"q": "str", "limit": "int", "include_archived": "bool"},
    surfaces=("cli", "mcp", "agent_tool"),
    side_effects=False,
    idempotent=True,
    cost_class="lookup",
    risk="read",
)


# -- `axi chat history` --------------------------------------------------------------


def render(value: dict[str, Any]) -> str:
    rows = value.get("sessions") or []
    if not rows:
        what = f' matching "{value["query"]}"' if value.get("query") else ""
        return f"No chat sessions{what}."
    from axiom.infra.branding import get_branding
    from axiom.infra.cli_format import Column, table

    body = "\n".join(
        table(
            [
                [
                    r["id"],
                    (r.get("updated_at") or "")[:16].replace("T", " "),
                    (r.get("title") or "Untitled chat")
                    + (f" ({r['snippet']})" if r.get("snippet") else ""),
                ]
                for r in rows
            ],
            [Column("id"), Column("updated"), Column("title", wrap=True)],
        )
    )
    return f"{body}\n\nNext: resume one with `{get_branding().cli_name} chat --resume <id>`."


def cli_main(argv: list[str]) -> int:
    from axiom.infra.branding import get_branding
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
    from axiom.infra.skills import SkillRegistry

    p = argparse.ArgumentParser(
        prog=f"{get_branding().cli_name} chat history",
        description="Your chat sessions, newest first.",
    )
    p.add_argument("q", nargs="*", help="only sessions whose title or messages match")
    p.add_argument("-n", "--limit", type=int, default=50)
    p.add_argument("--archived", action="store_true", help="include archived sessions")
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    registry = SkillRegistry()
    registry.register_skill(SPEC, mutating=False)
    ctx = SkillContext(
        registry=registry, state_dir=get_user_state_dir(), logger=logging.getLogger("axi.chat")
    )
    params = {"q": " ".join(args.q), "limit": args.limit, "include_archived": args.archived}
    result = invoke_capability(registry, NAME, params, ctx, surface=CLI_SURFACE)
    if not result.ok:
        print("\n".join(result.errors), file=sys.stderr)
        return result.exit_code or 1
    print(json.dumps(result.value, indent=2, default=str) if args.json else render(result.value))
    return 0


__all__ = ["NAME", "SPEC", "cli_main", "history", "render", "variant_bound"]
