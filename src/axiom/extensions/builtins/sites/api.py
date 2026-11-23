# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The sites extension's two web faces, each through the dispatch chokepoint so the
authority gate, audit and telemetry apply to a browser as to the CLI and MCP.

``GET /api/v1/first-run`` (``register_routes``) is the web face of
``sites.checklist`` with ``kind=first_run``. The person is the gate session's;
with no session the saved-chart step reads ``todo`` (it is somebody's catalogue).

``/api/v1/sites`` (``register_site_routes``) is what the scope selector lists
(MVP-15), over ``sites.list``. ``GET /api/v1/sites`` is the signed-in person's
sites, the choice they made and whether there is anything to choose.
``GET /api/v1/sites/{site}`` is one of them, and 404 for a site they may not see
exactly as for one that is not hosted. The choice itself travels as the
``axiom_scope`` cookie the selector sets (or the ``X-Axiom-Scope`` header), which
the HTTP middleware reads on every request, so every page and API narrows to it
without being taught to.

No ``from __future__ import annotations``: FastAPI resolves handler annotations
at registration and ``Request`` is imported inside ``register_routes``.
"""

import logging
from typing import Any


def _claims(request: Any) -> dict:
    try:
        from axiom.webauth import session_from_cookies

        return session_from_cookies(dict(request.cookies)) or {}
    except Exception:  # noqa: BLE001 - no session: the person-bound step stays todo
        return {}


def register_routes(router: Any, *, subpath: str = "/first-run") -> None:
    from fastapi import HTTPException, Request

    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skill_dispatch import WEB_SURFACE, invoke_capability
    from axiom.infra.skills import SkillContext

    from .skills import bind_default

    @router.get(subpath, tags=["sites"])
    def first_run(request: Request) -> dict:
        claims = _claims(request)
        registry = bind_default()
        ctx = SkillContext(
            registry=registry, state_dir=get_user_state_dir(), logger=logging.getLogger("axi.sites")
        )
        result = invoke_capability(
            registry,
            "sites.checklist",
            {"kind": "first_run", "_principal": str(claims.get("sub") or "")},
            ctx,
            surface=WEB_SURFACE,
        )
        if not result.ok:
            raise HTTPException(500, "; ".join(result.errors))
        return {k: v for k, v in result.value.items() if k != "text"}


def _call(name: str, params: dict[str, Any]) -> Any:
    from fastapi import HTTPException

    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skill_dispatch import WEB_SURFACE, invoke_capability
    from axiom.infra.skills import SkillContext, SkillRegistry

    from .skills import bind

    registry = SkillRegistry()
    bind(registry)
    ctx = SkillContext(registry=registry, state_dir=get_user_state_dir(),
                       logger=logging.getLogger("axi.sites"))
    result = invoke_capability(registry, name, params, ctx, surface=WEB_SURFACE)
    if not result.ok:
        raise HTTPException(404, "; ".join(result.errors) or "not found")
    return result.value


def register_site_routes(router: Any, *, subpath: str = "/sites") -> None:
    @router.get(subpath, tags=["sites"])
    def list_sites() -> dict:
        return _call("sites.list", {})

    @router.get(subpath + "/{site}", tags=["sites"])
    def show_site(site: str) -> dict:
        return _call("sites.show", {"site": site})
