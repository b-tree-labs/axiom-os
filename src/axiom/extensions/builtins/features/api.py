# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/api/v1/features`` and ``/api/v1/flags``: the web face of the one registry (ADR-210, ADR-254, amended 2026-11-12).

``/api/v1/flags`` is the canonical flags endpoint (appkit's ``FlagProvider`` reads it):

- ``GET /flags``: the signed-in caller's resolved flag map (``on|off|soon``), where
  each value came from, the variant values, what waits on an off extension, the flag
  metadata and which scopes this caller may set.
- ``PATCH /flags/{key}`` with ``{"value": "on|off|soon|clear", "scope": ..., "reason"?, "expires_at"?}``:
  ``user`` scope sets the caller's own override and is open to anyone signed in;
  ``account`` scope is for owners and site admins; ``node`` scope needs the
  ``site-admin`` role (or ``admin``). A protected flag is 403, an undeclared one 404. A flag whose extension is
  off here is stored and waits on that extension (``waiting_on``).
- ``GET /flags/{key}/explain``: why the flag has its value for the signed-in caller
  (LOOP-F8); a site admin may name another ``?user=`` / ``?account=``.
- ``POST /flags/{key}/kill`` with ``{"reason": ..., "engaged"?: true, "environment"?: ...}``:
  the kill switch (LOOP-F6), for site admins. Cohort and rollout edits are not on the
  web: they are destructive and run through a person's approved plan (CLI, MCP).

``/api/v1/features`` returns the whole registry, for node setup and the console:
everything ``GET /flags`` returns, plus ``switches`` (every row of the three kinds,
extension, function and flag), ``kinds``, ``scopes``, ``node`` and ``drift``.
``PATCH /features/{key}`` sets an extension or function at node scope (site
admins only), or a flag exactly as ``PATCH /flags/{key}`` does; ``/features/{key}/explain``
and ``/features/{key}/kill`` answer as the ``/flags`` ones do.

The caller is the session: ``sub`` is the user, ``site`` the account, this node
the node, and the session's roles and the browser's language are what cohort rules
read as ``role`` and ``locale`` (resolved on the server, LOOP-F12). Everything goes
through ``invoke_capability``, so a browser is gated, audited and measured the same
as the CLI and MCP.

No ``from __future__ import annotations``: FastAPI resolves handler annotations
at registration and ``Request`` is imported inside ``register_routes``.
"""

import logging
from typing import Any

#: Roles by their current ids; a session's roles are read through ``canonical_roles``, so a
#: grant stored as ``operator`` counts as ``site-admin`` (ADR-261).
ACCOUNT_ROLES = frozenset({"owner", "site-admin", "admin"})
NODE_ROLES = frozenset({"site-admin", "admin"})


def _claims(request: Any) -> dict:
    try:
        from axiom.webauth import session_from_cookies

        return session_from_cookies(dict(request.cookies)) or {}
    except Exception:  # noqa: BLE001 - no session is "not signed in"
        return {}


def _ctx(actor: str | None = None):
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skills import SkillContext, SkillRegistry

    from .skills import bind

    registry = SkillRegistry()
    bind(registry)
    return SkillContext(
        registry=registry,
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.features"),
        actor=actor,
    )


def _account(claims: dict) -> str | None:
    from .skills import default_account

    return str(claims.get("site") or "") or default_account()


def _actor(claims: dict) -> str:
    sub = str(claims.get("sub") or "unknown")
    site = str(claims.get("site") or "")
    return f"@{sub}:{site}" if site else f"@{sub}"


def may_set(roles: set[str]) -> dict[str, bool]:
    from axiom.governance.roles import canonical_roles

    roles = set(canonical_roles(roles))
    return {
        "user": True,
        "account": bool(ACCOUNT_ROLES & roles),
        "node": bool(NODE_ROLES & roles),
    }


def _status_for(errors: list[str]) -> int:
    text = "; ".join(errors)
    if "no flag" in text or "nothing on this node" in text:
        return 404
    if "protected" in text:
        return 403
    if "which is off on this node" in text:
        return 409
    if "is a feature flag" in text:
        return 400
    return 400


def _signed_in(claims: dict, HTTPException) -> None:
    if not claims:
        raise HTTPException(401, "sign in first")


def _locale(request: Any) -> str:
    try:
        raw = str(request.headers.get("accept-language") or "")
    except Exception:  # noqa: BLE001
        return ""
    return raw.split(",")[0].split(";")[0].strip()


def _who(claims: dict, request: Any = None) -> dict:
    """The subject the session resolves as: always from the session, never from the body."""
    return {
        "user": str(claims.get("sub") or ""),
        "account": _account(claims) or "",
        "roles": [str(r) for r in (claims.get("roles") or [])],
        "principal_kind": "person",
        "locale": _locale(request) if request is not None else "",
        # LOOP-F15: the principal's place, from the standard OIDC claims when the session has them.
        "timezone": str(claims.get("zoneinfo") or ""),
        "country": str(_address(claims).get("country") or ""),
        "region": str(_address(claims).get("region") or ""),
    }


def _address(claims: dict) -> dict:
    address = claims.get("address")
    return address if isinstance(address, dict) else {}


def _resolved(claims: dict, *, switches: bool, request: Any = None) -> dict:
    """The caller's flag map, metadata and scopes; with ``switches`` the whole registry."""
    from fastapi import HTTPException

    from axiom.infra.skill_dispatch import WEB_SURFACE, invoke_capability

    ctx = _ctx(_actor(claims))
    who = _who(claims, request)
    resolved = invoke_capability(ctx.registry, "flags.resolve", who, ctx, surface=WEB_SURFACE)
    flags = invoke_capability(ctx.registry, "flags.list", {"user": who["user"], "account": who["account"]},
                              ctx, surface=WEB_SURFACE)
    if not (resolved.ok and flags.ok):
        raise HTTPException(500, "; ".join(resolved.errors + flags.errors))
    flag_rows = flags.value["rows"]
    metadata = {
        row["name"]: {
            "description": row["description"],
            "default": row["details"].get("default"),
            "protected": bool(row["details"].get("protected")),
            "owner": row["owner"],
        }
        for row in flag_rows
    }
    from axiom.infra.variants import active_variant

    variant = active_variant()
    body = {
        # Which web app variant answered (``None`` on a plain install), so
        # a page and a smoke can tell the three apart (VAR-A4).
        "variant": variant.name if variant else None,
        "flags": resolved.value["flags"],
        # key -> the value of the variant a multivariate flag resolved to (LOOP-F17).
        "values": resolved.value.get("values") or {},
        "sources": resolved.value["sources"],
        # key -> the off extension a flag waits on ("waiting on extension <name>").
        "waiting_on": resolved.value.get("waiting_on") or {},
        "environment": resolved.value["environment"],
        "metadata": metadata,
        "can_set": may_set(set(claims.get("roles") or [])),
    }
    if switches:
        listed = invoke_capability(ctx.registry, "features.list", {}, ctx, surface=WEB_SURFACE)
        if not listed.ok:
            raise HTTPException(500, "; ".join(listed.errors))
        body["switches"] = [*listed.value["rows"], *flag_rows]
        body["kinds"] = listed.value["kinds"]
        body["scopes"] = listed.value["scopes"]
        body["node"] = {
            k: listed.value[k]
            for k in ("role", "functions", "confined", "agent_policy", "maintenance", "record_of_truth")
        }
        body["drift"] = listed.value["drift"]
    return body


def _scope_id_for(claims: dict, scope: str, HTTPException) -> str:
    if scope == "user":
        scope_id = str(claims.get("sub") or "")
    elif scope == "account":
        scope_id = _account(claims) or ""
    else:
        scope_id = ""  # the skill fills in this node
    if not scope_id and scope != "node":
        raise HTTPException(400, f"this session names no {scope}")
    return scope_id


def _check_scope(claims: dict, scope: str, HTTPException) -> None:
    allowed = may_set(set(claims.get("roles") or []))
    if scope not in allowed:
        raise HTTPException(400, "scope is user, account or node")
    if not allowed[scope]:
        who = "a site admin" if scope == "node" else "the owner or a site admin"
        raise HTTPException(403, f"only {who} sets a value for the whole {scope}")


def _is_flag(key: str, kind: str | None) -> bool:
    """Whether ``key`` is set as a flag (the scoped rules) or as a node switch."""
    if kind:
        return kind == "flag"
    try:
        from .resolver import FlagService

        return key in FlagService().get_registry()
    except Exception:  # noqa: BLE001
        return False


def _flag_routes(router: Any, subpath: str, tag: str) -> None:
    """``GET {subpath}/{key}/explain`` and ``POST {subpath}/{key}/kill``: the same on both faces."""
    from fastapi import HTTPException, Request
    from pydantic import BaseModel

    from axiom.infra.skill_dispatch import WEB_SURFACE, invoke_capability

    class KillBody(BaseModel):
        reason: str = ""
        engaged: bool = True
        environment: str | None = None

    switches = subpath.rstrip("/").endswith("/features")

    @router.get(subpath + "/{key}/explain", tags=[tag])
    def explain_flag(key: str, request: Request, user: str = "", account: str = "") -> dict:
        claims = _claims(request)
        _signed_in(claims, HTTPException)
        params = {"key": key, **_who(claims, request)}
        own = {params["user"], ""}, {params["account"], ""}
        if user not in own[0] or account not in own[1]:
            if not may_set(set(claims.get("roles") or []))["node"]:
                raise HTTPException(403, "only a site admin explains a flag for somebody else")
            # Somebody else: what the server knows of them, not the site admin's own session.
            params = {"key": key, "user": user or params["user"], "account": account or params["account"]}
        ctx = _ctx(_actor(claims))
        result = invoke_capability(ctx.registry, "flags.explain", params, ctx, surface=WEB_SURFACE)
        if not result.ok:
            raise HTTPException(_status_for(result.errors), "; ".join(result.errors))
        return result.value

    @router.post(subpath + "/{key}/kill", tags=[tag])
    def kill_flag(key: str, request: Request, body: KillBody) -> dict:
        claims = _claims(request)
        _signed_in(claims, HTTPException)
        if not may_set(set(claims.get("roles") or []))["node"]:
            raise HTTPException(403, "only a site admin pulls a kill switch")
        from .person_only import stale_sign_in

        stale = stale_sign_in(key, claims)
        if stale:
            raise HTTPException(403, stale)
        ctx = _ctx(_actor(claims))
        result = invoke_capability(
            ctx.registry,
            "flags.kill",
            {"key": key, "reason": body.reason, "engaged": body.engaged, "environment": body.environment or ""},
            ctx,
            surface=WEB_SURFACE,
        )
        if not result.ok:
            raise HTTPException(_status_for(result.errors), "; ".join(result.errors))
        return {"kill": result.value, **_resolved(claims, switches=switches, request=request)}


def _set_flag(claims: dict, key: str, body: Any, *, switches: bool, request: Any) -> dict:
    """One flag override from the web, through ``flags.set``."""
    from fastapi import HTTPException

    from axiom.infra.skill_dispatch import WEB_SURFACE, invoke_capability

    from .person_only import stale_sign_in

    stale = stale_sign_in(key, claims)
    if stale:
        raise HTTPException(403, stale)
    scope = body.scope.strip().lower()
    _check_scope(claims, scope, HTTPException)
    scope_id = _scope_id_for(claims, scope, HTTPException)
    ctx = _ctx(_actor(claims))
    result = invoke_capability(
        ctx.registry,
        "flags.set",
        {"key": key, "value": body.value, "scope": scope, "scope_id": scope_id,
         "reason": body.reason or "", "expires_at": body.expires_at or ""},
        ctx,
        surface=WEB_SURFACE,
    )
    if not result.ok:
        raise HTTPException(_status_for(result.errors), "; ".join(result.errors))
    return {"set": result.value, **_resolved(claims, switches=switches, request=request)}


def register_routes(router: Any, *, subpath: str = "/features") -> None:
    """``/features``: the whole registry, and a node-scope set (a flag goes through ``flags.set``)."""
    from fastapi import HTTPException, Request
    from pydantic import BaseModel

    from axiom.infra.skill_dispatch import WEB_SURFACE, invoke_capability

    class PatchBody(BaseModel):
        value: Any
        scope: str = "user"
        kind: str | None = None
        reason: str | None = None
        expires_at: str | None = None

    @router.get(subpath, tags=["features"])
    def read_registry(request: Request) -> dict:
        claims = _claims(request)
        _signed_in(claims, HTTPException)
        return _resolved(claims, switches=True, request=request)

    _flag_routes(router, subpath, "features")

    @router.patch(subpath + "/{key}", tags=["features"])
    def set_value(key: str, request: Request, body: PatchBody) -> dict:
        claims = _claims(request)
        _signed_in(claims, HTTPException)
        kind = (body.kind or "").strip().lower() or None
        if _is_flag(key, kind):
            return _set_flag(claims, key, body, switches=True, request=request)
        # An extension or function changes what runs: node scope, site admins only.
        scope = body.scope.strip().lower()
        if scope == "user" and body.kind is None:
            scope = "node"
        _check_scope(claims, "node", HTTPException)
        ctx = _ctx(_actor(claims))
        result = invoke_capability(
            ctx.registry,
            "features.set",
            {"key": key, "value": body.value, "scope": scope, "scope_id": "", "kind": kind},
            ctx,
            surface=WEB_SURFACE,
        )
        if not result.ok:
            raise HTTPException(_status_for(result.errors), "; ".join(result.errors))
        return {"set": result.value, **_resolved(claims, switches=True, request=request)}


def register_flag_routes(router: Any, *, subpath: str = "/flags") -> None:
    """``/flags``: the canonical flags endpoint; the shape appkit's ``FlagProvider`` reads."""
    from fastapi import HTTPException, Request
    from pydantic import BaseModel

    class PatchBody(BaseModel):
        value: Any
        scope: str = "user"
        reason: str | None = None
        expires_at: str | None = None

    @router.get(subpath, tags=["flags"])
    def read_flags(request: Request) -> dict:
        claims = _claims(request)
        _signed_in(claims, HTTPException)
        return _resolved(claims, switches=False, request=request)

    _flag_routes(router, subpath, "flags")

    @router.patch(subpath + "/{key}", tags=["flags"])
    def set_flag(key: str, request: Request, body: PatchBody) -> dict:
        claims = _claims(request)
        _signed_in(claims, HTTPException)
        return _set_flag(claims, key, body, switches=False, request=request)


__all__ = ["ACCOUNT_ROLES", "NODE_ROLES", "may_set", "register_flag_routes", "register_routes"]
