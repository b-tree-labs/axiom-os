# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``POST /api/v1/next`` — the web face of ``next.suggest``.

POST because the request carries the view context (a structure, not a query
string). The person is the gate session's; with no session the offers are the
generic ones and no history is read. A sync handler: providers read stores.

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
    except Exception:  # noqa: BLE001 - no session: generic offers, no history
        return {}


def register_routes(router: Any, *, subpath: str = "/next") -> None:
    from fastapi import Request
    from pydantic import BaseModel

    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skill_dispatch import WEB_SURFACE, invoke_capability
    from axiom.infra.skills import SkillContext, SkillRegistry

    from .skills import bind

    class Body(BaseModel):
        view_context: dict | None = None
        limit: int = 5

    @router.post(subpath, tags=["next"])
    def suggest(request: Request, body: Body) -> dict:
        claims = _claims(request)
        registry = SkillRegistry()
        bind(registry)
        ctx = SkillContext(
            registry=registry, state_dir=get_user_state_dir(), logger=logging.getLogger("axi.next")
        )
        # Through the dispatch chokepoint, never around it: authority gate,
        # audit and telemetry apply to a browser exactly as to the CLI and MCP.
        result = invoke_capability(
            registry,
            "next.suggest",
            {
                "view_context": body.view_context,
                "limit": body.limit,
                # Whose history may be read: the session's, or nobody's (empty).
                "_principal": str(claims.get("sub") or ""),
                "_roles": list(claims.get("roles") or []),
            },
            ctx,
            surface=WEB_SURFACE,
        )
        return result.value
