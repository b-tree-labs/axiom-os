# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/api/v1/flags``: the canonical flags endpoint (ADR-210, ADR-254 amended 2026-11-12).

The routes are registered by ``features.api.register_flag_routes``: ``GET /flags``
(the shape appkit's ``FlagProvider`` reads), ``PATCH /flags/{key}``,
``GET /flags/{key}/explain`` and ``POST /flags/{key}/kill``.
``/api/v1/features`` returns the whole registry beside it. ``GET /flags/stale`` is the
flag lifecycle report (LOOP-F11), for site admins only (403 for anyone else).
"""

from typing import Any

from axiom.extensions.builtins.features.api import (
    ACCOUNT_ROLES,
    NODE_ROLES,
    may_set,
    register_flag_routes,
)


def _claims(request: Any) -> dict:
    from axiom.extensions.builtins.features.api import _claims as claims

    return claims(request)


def _stale_ctx(actor: str):
    import logging

    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skills import SkillContext, SkillRegistry

    from .skills import bind

    registry = SkillRegistry()
    bind(registry)
    return SkillContext(registry=registry, state_dir=get_user_state_dir(), logger=logging.getLogger("axi.flags"),
                        actor=actor)


def register_routes(router: Any, *, subpath: str = "/flags") -> None:
    from fastapi import HTTPException, Request

    from axiom.infra.skill_dispatch import WEB_SURFACE, invoke_capability

    # Registered before /flags/{key}/...: the stale report is a fixed path (LOOP-F11).
    @router.get(subpath + "/stale", tags=["flags"])
    def stale_flags(request: Request, environment: str = "", today: str = "") -> dict:
        from . import lifecycle
        from .skills.stale import is_site_admin, session_roles

        claims = _claims(request)
        if not claims:
            raise HTTPException(401, "sign in first")
        roles = list(claims.get("roles") or [])
        sub, site = str(claims.get("sub") or "unknown"), str(claims.get("site") or "")
        actor = f"@{sub}:{site}" if site else f"@{sub}"
        if not (is_site_admin(roles) or lifecycle.is_maintainer(actor, lifecycle.configured_maintainers())):
            # Read-only for site admins; the work belongs to the maintainers.
            raise HTTPException(403, "the stale-flag report is for site admins and maintainers")
        ctx = _stale_ctx(actor)
        with session_roles(roles):
            result = invoke_capability(ctx.registry, "flags.stale", {"environment": environment, "today": today},
                                       ctx, surface=WEB_SURFACE)
        if not result.ok:
            raise HTTPException(400, "; ".join(result.errors))
        return result.value

    register_flag_routes(router, subpath=subpath)


__all__ = ["ACCOUNT_ROLES", "NODE_ROLES", "may_set", "register_routes"]
