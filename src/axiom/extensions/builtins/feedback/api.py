# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/api/v1/feedback``: the web face of the feedback skills (LOOP-B1, B5, B6).

- ``POST /feedback`` ``{verdict, text?, severe?, screen?, flag?}``: one report from the
  signed-in person. The reporter and the subject flags are resolved for come from the
  session, never the body.
- ``POST /feedback/groups/{id}/decide`` ``{choice, reason?}``: the owner answers the ask.
- ``GET /feedback/flags/{key}``: a flag's reports grouped by cause, for the flag's People
  view (site-admin role).

Through the dispatch chokepoint like every other surface. No ``from __future__ import
annotations``: FastAPI resolves handler annotations at registration.
"""

import logging
from typing import Any

from axiom.governance.roles import SITE_ADMIN, canonical_roles


def _claims(request: Any) -> dict:
    try:
        from axiom.webauth import session_from_cookies

        return session_from_cookies(dict(request.cookies)) or {}
    except Exception:  # noqa: BLE001 - no session is "not signed in"
        return {}


def register_routes(router: Any, *, subpath: str = "/feedback") -> None:
    from fastapi import HTTPException, Request
    from pydantic import BaseModel

    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skill_dispatch import WEB_SURFACE, invoke_capability
    from axiom.infra.skills import SkillContext, SkillRegistry

    from .skills import bind

    class ReportBody(BaseModel):
        verdict: str
        text: str | None = None
        severe: bool = False
        screen: str | None = None
        flag: str | None = None

    class DecideBody(BaseModel):
        choice: str
        reason: str | None = None

    def _person(request: Any) -> tuple[dict, str]:
        claims = _claims(request)
        if not claims:
            raise HTTPException(401, "sign in first")
        from axiom.extensions.builtins.settings.api import principal_of

        person = principal_of(claims) or (f"@{claims['sub']}" if claims.get("sub") else "")
        if not person:
            raise HTTPException(403, "this sign-in carries no name to report as")
        return claims, person

    def _run(capability: str, params: dict, person: str) -> Any:
        registry = SkillRegistry()
        bind(registry)
        ctx = SkillContext(registry=registry, state_dir=get_user_state_dir(),
                           logger=logging.getLogger("axi.feedback"), actor=person)
        result = invoke_capability(registry, capability, params, ctx, surface=WEB_SURFACE)
        if not result.ok:
            raise HTTPException(400, "; ".join(result.errors))
        return result.value

    def _roles(claims: dict) -> list[str]:
        return [str(r) for r in (claims.get("roles") or [])]

    @router.post(subpath, tags=["feedback"])
    def post_report(request: Request, body: ReportBody) -> dict:
        claims, person = _person(request)
        subject = {"user": str(claims.get("sub") or ""), "account": str(claims.get("site") or ""),
                   "roles": _roles(claims), "principal_kind": "person"}
        return _run("feedback.report", {**body.model_dump(exclude_none=True), "_subject": subject}, person)

    @router.post(subpath + "/groups/{group_id}/decide", tags=["feedback"])
    def post_decide(request: Request, group_id: str, body: DecideBody) -> dict:
        claims, person = _person(request)
        return _run("feedback.decide", {"group": group_id, "choice": body.choice, "reason": body.reason or "",
                                        "_roles": _roles(claims)}, person)

    @router.get(subpath + "/flags/{key}", tags=["feedback"])
    def get_flag_reports(request: Request, key: str, limit: int = 20) -> dict:
        claims, person = _person(request)
        if SITE_ADMIN not in canonical_roles(_roles(claims)):
            raise HTTPException(403, "a flag's reports are for the site admins")
        return _run("feedback.reports", {"flag": key, "limit": limit}, person)


__all__ = ["register_routes"]
