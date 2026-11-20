# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``GET /api/v1/needs-you`` — the attention bar's queue for the signed-in person.

The person comes from the session, never from the request, so the chip shows
only the caller's own items (MVP-21-A1). Through the dispatch chokepoint like
every other surface: the same skill chat and MCP call.

No ``from __future__ import annotations``: FastAPI resolves handler annotations
at registration and ``Request`` is imported inside ``register_routes``.
"""

import logging
from typing import Any


def _claims(request: Any) -> dict:
    try:
        from axiom.webauth import session_from_cookies

        return session_from_cookies(dict(request.cookies)) or {}
    except Exception:  # noqa: BLE001 - no session is "not signed in"
        return {}


def register_routes(router: Any, *, subpath: str = "/needs-you") -> None:
    from fastapi import HTTPException, Request

    from axiom.infra.skill_dispatch import WEB_SURFACE, invoke_capability

    @router.get(subpath, tags=["notifications"])
    def read_needs_you(request: Request, limit: int = 50) -> dict:
        claims = _claims(request)
        if not claims:
            raise HTTPException(401, "sign in first")
        from axiom.extensions.builtins.settings.api import principal_of
        from axiom.infra.paths import get_user_state_dir
        from axiom.infra.skills import SkillContext, SkillRegistry

        from .skills import bind

        person = principal_of(claims)
        if not person:
            raise HTTPException(403, "this sign-in carries no name to address a queue to")
        registry = SkillRegistry()
        bind(registry)
        ctx = SkillContext(registry=registry, state_dir=get_user_state_dir(),
                           logger=logging.getLogger("axi.needs_you"), actor=person)
        result = invoke_capability(registry, "notifications.needs_you", {"limit": limit}, ctx,
                                   surface=WEB_SURFACE)
        if not result.ok:
            raise HTTPException(500, "; ".join(result.errors))
        return result.value
