# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/api/v1/schedule/agenda`` and ``/api/v1/schedule/items/{id}``: the agenda read
for a signed-in browser (MVP-24).

Both go through the dispatch chokepoint with ``WEB_SURFACE``, the same skill
the CLI, MCP and chat call, so the four surfaces return the same items. Read
only: nothing here writes to any source.

No ``from __future__ import annotations``: FastAPI resolves handler
annotations at registration and ``Request`` is imported inside
``register_routes``.
"""

import logging
from typing import Any


def _claims(request: Any) -> dict:
    try:
        from axiom.webauth import session_from_cookies

        return session_from_cookies(dict(request.cookies)) or {}
    except Exception:  # noqa: BLE001 - no session is "not signed in"
        return {}


def _ctx():
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skills import SkillContext, SkillRegistry

    from .skills import bind

    registry = SkillRegistry()
    bind(registry)
    return SkillContext(
        registry=registry,
        state_dir=get_user_state_dir() / "schedule",
        logger=logging.getLogger("axi.schedule"),
    )


def register_routes(router: Any, *, subpath: str = "/schedule") -> None:
    from fastapi import HTTPException, Request

    from axiom.infra.skill_dispatch import WEB_SURFACE, invoke_capability

    @router.get(f"{subpath}/agenda", tags=["schedule"])
    def read_agenda(
        request: Request,
        days: int | None = None,
        start: str | None = None,
        end: str | None = None,
        source: str | None = None,
        person: str | None = None,
    ) -> dict:
        if not _claims(request):
            raise HTTPException(401, "sign in first")
        # `from` / `to` are the skill's names; `from` is not a Python identifier,
        # so the query string accepts it alongside `start` / `end`.
        q = request.query_params
        params = {
            "days": days,
            "from": q.get("from") or start,
            "to": q.get("to") or end,
            "source": source,
            "person": person,
        }
        params = {k: v for k, v in params.items() if v not in (None, "")}
        ctx = _ctx()
        result = invoke_capability(ctx.registry, "schedule.items_list", params, ctx, surface=WEB_SURFACE)
        if not result.ok:
            raise HTTPException(400, "; ".join(result.errors))
        return result.value

    @router.get(f"{subpath}/items/{{item_id:path}}", tags=["schedule"])
    def read_item(request: Request, item_id: str) -> dict:
        if not _claims(request):
            raise HTTPException(401, "sign in first")
        ctx = _ctx()
        result = invoke_capability(ctx.registry, "schedule.items_show", {"id": item_id}, ctx,
                                   surface=WEB_SURFACE)
        if not result.ok:
            raise HTTPException(404, "; ".join(result.errors))
        return result.value


__all__ = ["register_routes"]
