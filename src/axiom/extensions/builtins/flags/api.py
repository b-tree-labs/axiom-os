# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/api/v1/flags``: the web face over the flags skills (ADR-210).

- ``GET /flags``: the signed-in caller's resolved map (``on|off|soon``), where
  each value came from, the flag metadata, and which scopes this caller may set.
- ``PATCH /flags/{key}`` with ``{"value": "on|off|soon|clear", "scope": ...}``:
  ``user`` scope sets the caller's own override and is open to anyone signed
  in; ``account`` scope is for owners and operators; ``node`` scope needs the
  operator role. A protected flag is 403, an undeclared one 404 (SoilMetrix's
  route semantics).

The caller is the session: ``sub`` is the user, ``site`` the account, this node
the node. Everything goes through ``invoke_capability``, so a browser is gated,
audited and measured the same as the CLI and MCP.

No ``from __future__ import annotations``: FastAPI resolves handler annotations
at registration and ``Request`` is imported inside ``register_routes``.
"""

import logging
from typing import Any

ACCOUNT_ROLES = frozenset({"owner", "operator", "admin"})
NODE_ROLES = frozenset({"operator", "admin"})


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
        logger=logging.getLogger("axi.flags"),
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
    return {
        "user": True,
        "account": bool(ACCOUNT_ROLES & roles),
        "node": bool(NODE_ROLES & roles),
    }


def register_routes(router: Any, *, subpath: str = "/flags") -> None:
    from fastapi import HTTPException, Request
    from pydantic import BaseModel

    from axiom.infra.skill_dispatch import WEB_SURFACE, invoke_capability

    class PatchBody(BaseModel):
        value: Any
        scope: str = "user"

    def _resolved(claims: dict) -> dict:
        ctx = _ctx(_actor(claims))
        who = {"user": str(claims.get("sub") or ""), "account": _account(claims) or ""}
        resolved = invoke_capability(ctx.registry, "flags.resolve", who, ctx, surface=WEB_SURFACE)
        listed = invoke_capability(ctx.registry, "flags.list", {}, ctx, surface=WEB_SURFACE)
        if not (resolved.ok and listed.ok):
            raise HTTPException(500, "; ".join(resolved.errors + listed.errors))
        metadata = {
            row["key"]: {k: row[k] for k in ("description", "default", "protected", "owner")}
            for row in listed.value["flags"]
        }
        return {
            "flags": resolved.value["flags"],
            "sources": resolved.value["sources"],
            "environment": resolved.value["environment"],
            "metadata": metadata,
            "can_set": may_set(set(claims.get("roles") or [])),
        }

    @router.get(subpath, tags=["flags"])
    def read_flags(request: Request) -> dict:
        claims = _claims(request)
        if not claims:
            raise HTTPException(401, "sign in first")
        return _resolved(claims)

    @router.patch(subpath + "/{key}", tags=["flags"])
    def set_flag(key: str, request: Request, body: PatchBody) -> dict:
        claims = _claims(request)
        if not claims:
            raise HTTPException(401, "sign in first")
        scope = body.scope.strip().lower()
        allowed = may_set(set(claims.get("roles") or []))
        if scope not in allowed:
            raise HTTPException(400, "scope is user, account or node")
        if not allowed[scope]:
            who = "an operator" if scope == "node" else "the owner or an operator"
            raise HTTPException(403, f"only {who} sets a flag for the whole {scope}")
        if scope == "user":
            scope_id = str(claims.get("sub") or "")
        elif scope == "account":
            scope_id = _account(claims) or ""
        else:
            scope_id = ""  # the skill fills in this node
        if not scope_id and scope != "node":
            raise HTTPException(400, f"this session names no {scope}")
        ctx = _ctx(_actor(claims))
        result = invoke_capability(
            ctx.registry,
            "flags.set",
            {"key": key, "value": body.value, "scope": scope, "scope_id": scope_id},
            ctx,
            surface=WEB_SURFACE,
        )
        if not result.ok:
            text = "; ".join(result.errors)
            status = 404 if "no flag" in text else 403 if "protected" in text else 400
            raise HTTPException(status, text)
        return {"set": result.value, **_resolved(claims)}
