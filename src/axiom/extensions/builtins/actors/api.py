# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/api/v1/actors``: the Team page's door (MVP-23), over the actors and gate skills.

Reads:

- ``GET /api/v1/actors?kind=&role=`` — ``actors.list`` for the signed-in person,
  with the fields their role may see.
- ``GET /api/v1/actors/{handle}`` — ``actors.show``; 404 for an actor they may not
  see, exactly as for one that is not here.

Writes, for administrators only, are the gate's own verbs (so each is audited
once, by the dispatch chokepoint, like the same verb at the terminal):

- ``POST /api/v1/actors/invite`` → ``gate.invite`` (the code is in the reply, once);
- ``POST /api/v1/actors/roles`` → ``gate.role``;
- ``POST /api/v1/actors/suspend`` and ``/reinstate`` → ``gate.suspend`` / ``gate.reinstate``.

An administrator held to a site invites into that site and changes only accounts
of that site. Nobody signs in again because of any of these: the gate reads the
account live on the next request.
"""

# No `from __future__ import annotations`: FastAPI resolves handler annotations
# at registration, and `Request` is imported inside register_routes.
import logging
from typing import Any


def _claims(request: Any) -> dict:
    try:
        from axiom.webauth import session_from_cookies

        return session_from_cookies(dict(request.cookies)) or {}
    except Exception:  # noqa: BLE001 - no session is "not signed in"
        return {}


def _registry():
    from axiom.extensions.builtins.webgate import skills as gate_skills

    from .skills import bind

    from axiom.infra.skills import SkillRegistry

    reg = SkillRegistry()
    bind(reg)
    gate_skills.bind(reg)
    return reg


def _invoke(name: str, params: dict[str, Any], claims: dict) -> Any:
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skill_dispatch import WEB_SURFACE, invoke_capability
    from axiom.infra.skills import SkillContext

    reg = _registry()
    sub = str(claims.get("sub") or "")
    site = str(claims.get("site") or "")
    actor = (f"@{sub}:{site}" if site else f"@{sub}") if sub else None
    ctx = SkillContext(registry=reg, state_dir=get_user_state_dir(),
                       logger=logging.getLogger("axi.actors"), actor=actor)
    return invoke_capability(reg, name, params, ctx, surface=WEB_SURFACE)


def register_routes(router: Any, *, subpath: str = "/actors") -> None:
    from fastapi import HTTPException, Query, Request
    from pydantic import BaseModel

    from .visibility import ADMIN, viewer_class

    class InviteBody(BaseModel):
        handle: str = ""
        name: str = ""
        role: str
        expires: str = "7d"

    class RolesBody(BaseModel):
        email: str
        set: list[str] | None = None
        add: list[str] | None = None
        remove: list[str] | None = None

    class LifecycleBody(BaseModel):
        email: str
        reason: str = ""

    def _signed_in(request: Request) -> dict:
        claims = _claims(request)
        if not claims:
            raise HTTPException(401, "sign in first")
        return claims

    def _admin(request: Request) -> dict:
        claims = _signed_in(request)
        if viewer_class(claims.get("roles") or []) != ADMIN:
            raise HTTPException(403, "only an administrator invites people and changes roles")
        return claims

    def _account_in_scope(email: str, claims: dict) -> None:
        """An administrator held to a site manages that site's accounts only."""
        own = str(claims.get("site") or "")
        if not own:
            return
        from axiom.webauth import load_user_records
        from axiom.webauth.live import accounts_path

        path = accounts_path()
        records = load_user_records(path) if path and path.is_file() else []
        rec = next((r for r in records if r["email"] == email.strip().lower()), None)
        if rec is None or (rec.get("site") or "") != own:
            raise HTTPException(404, f"no account {email!r} here")

    def _result(result) -> dict:
        if not result.ok:
            raise HTTPException(400, "; ".join(result.errors) or "refused")
        return {"ok": True, "value": result.value, "actions_taken": result.actions_taken}

    @router.get(subpath, tags=["actors"])
    def list_actors(request: Request, kind: str | None = Query(None),
                    role: str | None = Query(None)) -> dict:
        claims = _signed_in(request)
        result = _invoke("actors.list", {"kind": kind, "role": role,
                                         "_viewer_roles": list(claims.get("roles") or [])}, claims)
        if not result.ok:
            raise HTTPException(400, "; ".join(result.errors))
        return result.value

    @router.get(subpath + "/{handle:path}", tags=["actors"])
    def show_actor(handle: str, request: Request) -> dict:
        claims = _signed_in(request)
        result = _invoke("actors.show", {"handle": handle,
                                         "_viewer_roles": list(claims.get("roles") or [])}, claims)
        if not result.ok:
            raise HTTPException(404, "; ".join(result.errors))
        return result.value

    @router.post(subpath + "/invite", tags=["actors"])
    def invite(request: Request, body: InviteBody) -> dict:
        claims = _admin(request)
        site = str(claims.get("site") or "") or None
        handle = body.handle.strip()
        if not handle:
            raise HTTPException(400, "name the person: handle is @name or @name:site")
        if not handle.startswith("@"):
            handle = "@" + handle
        name_part, _, asked_site = handle[1:].partition(":")
        if site and asked_site and asked_site != site:
            raise HTTPException(403, f"you invite people to {site!r} only")
        params = {"principal": f"@{name_part}", "role": [body.role], "name": body.name,
                  "expires": body.expires, "site": site or asked_site or None,
                  "invited_by": str(claims.get("email") or claims.get("sub") or "")}
        return _result(_invoke("gate.invite", params, claims))

    @router.post(subpath + "/roles", tags=["actors"])
    def set_roles(request: Request, body: RolesBody) -> dict:
        claims = _admin(request)
        _account_in_scope(body.email, claims)
        params = {"email": body.email, "set": body.set, "add": body.add, "remove": body.remove}
        return _result(_invoke("gate.role", {k: v for k, v in params.items() if v}, claims))

    @router.post(subpath + "/suspend", tags=["actors"])
    def suspend(request: Request, body: LifecycleBody) -> dict:
        claims = _admin(request)
        _account_in_scope(body.email, claims)
        if body.email.strip().lower() == str(claims.get("email") or "").lower():
            raise HTTPException(400, "you cannot suspend yourself")
        return _result(_invoke("gate.suspend", {"email": body.email, "reason": body.reason,
                                                "by": str(claims.get("email") or "")}, claims))

    @router.post(subpath + "/reinstate", tags=["actors"])
    def reinstate(request: Request, body: LifecycleBody) -> dict:
        claims = _admin(request)
        _account_in_scope(body.email, claims)
        return _result(_invoke("gate.reinstate", {"email": body.email}, claims))
