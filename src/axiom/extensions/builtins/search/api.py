# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/api/v1/search`` — the web face of ``search.query``.

Two routes, as the reference has: ``/suggest`` for the palette (fast providers,
eight rows) and the bare route for the results page. Both are SYNC handlers so
FastAPI runs them off the event loop: a provider may do blocking I/O.

The asker's bound comes from the gate session (the principal) and the node's
site scope; the skill applies it. Nothing here widens it.
"""

# No `from __future__ import annotations` here: FastAPI resolves handler
# annotations at registration, and `Request` is imported inside
# register_routes — a string annotation it cannot resolve turns `request`
# into a required QUERY parameter (422 on every call).
import logging
from typing import Any

from axiom.infra.skills import SkillContext, SkillRegistry

from .skills import bind


def _principal(request) -> str:
    try:
        from axiom.webauth import session_from_cookies

        claims = session_from_cookies(dict(request.cookies))
        return str((claims or {}).get("sub") or "")
    except Exception:  # noqa: BLE001 - no session narrows nothing; site scope still bounds
        return ""


def _call(request: Any, params: dict[str, Any]) -> Any:
    from fastapi import HTTPException

    from axiom.infra.paths import get_user_state_dir

    registry = SkillRegistry()
    bind(registry)
    ctx = SkillContext(
        registry=registry, state_dir=get_user_state_dir(), logger=logging.getLogger("axi.search")
    )
    # Through the dispatch chokepoint, never around it: authority gate, audit
    # and telemetry apply to a browser exactly as to the CLI and MCP.
    from axiom.infra.skill_dispatch import WEB_SURFACE, invoke_capability

    result = invoke_capability(
        registry,
        "search.query",
        {**params, "_principal": _principal(request)},
        ctx,
        surface=WEB_SURFACE,
    )
    if not result.ok:
        raise HTTPException(400, "; ".join(result.errors))
    return result.value


def register_routes(router: Any, *, subpath: str = "/search") -> None:
    from fastapi import Query, Request

    @router.get(subpath, tags=["search"])
    def search(
        request: Request,
        q: str = Query(..., min_length=1, max_length=500),
        kinds: str | None = Query(None),
        limit: int = Query(20, ge=1, le=50),
        include_archived: bool = Query(False),
    ) -> dict:
        return _call(
            request,
            {
                "query": q,
                "kinds": kinds,
                "limit": limit,
                "include_archived": include_archived,
                "mode": "full",
            },
        )

    @router.get(subpath + "/suggest", tags=["search"])
    def suggest(
        request: Request,
        q: str = Query("", max_length=200),
        limit: int = Query(8, ge=1, le=20),
    ) -> dict:
        if len(q.strip()) < 2:
            return {"query": q.strip(), "mode": "suggest", "results": [], "skipped": {}}
        return _call(request, {"query": q, "limit": limit, "mode": "suggest"})
