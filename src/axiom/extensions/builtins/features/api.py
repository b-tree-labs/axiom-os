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
- ``GET /flags/{key}/explain``: the flag's value for the signed-in caller and one plain
  sentence about it, with the site admin to ask (SW-A18); a site admin also gets the
  full ``trace`` (LOOP-F8) and may name another ``?user=`` / ``?account=``.
- ``POST /flags/{key}/kill`` with ``{"reason": ..., "engaged"?: true, "environment"?: ..., "message"?: ...}``:
  the kill switch (LOOP-F6), for site admins.

The feature-flag screens (SW-A5, A13 to A19, A23, A25), every one for a site admin,
on ``/flags`` only. Each write runs the same ``flags.*`` skill the CLI and MCP run, on
the web surface, so a destructive one is planned and runs only through a signed plan:

- ``GET /flags/admin``, ``GET /flags/cohorts``, ``GET /flags/changes``: what the screens list.
- ``POST /flags/{key}/stop`` and ``/release``: the kill switch with a public message kept
  apart from the internal reason; never held.
- ``POST /flags/{key}/rollout/preview`` then ``/rollout/apply`` (with the ``plan_hash``);
  the same pair for ``/flags/cohorts/{key}`` and ``/flags/cohorts/{key}/import``. Apply
  approves the held plan as the signed-in site admin and runs it (Change, then Apply);
  a change the site's policy holds answers 202 and waits for a second site admin's approval.
- ``POST /flags/changes/{id}/approve`` (``/cosign`` is an alias) and ``/withdraw``; ``GET /flags/waiting``;
  ``POST /flags/{key}/stages/preview``, ``/apply`` and ``/halt``; the rule builder: drafts, a live count,
  the attribute vocabulary and the values to pick.

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


#: The node policies ``PATCH /features/{key}`` sets with ``{value}``: each one's verb and its parameter.
POLICY_SKILLS = {
    "maintenance": ("features.maintenance", "level"),
    "record-of-truth": ("features.record_of_truth", "truth"),
    "agents": ("features.agents", "policy"),
}


def is_site_admin(roles: Any) -> bool:
    """Whether a session's roles make it a site admin: ``site-admin``, or a grant still stored as
    ``operator`` (``canonical_roles``, ADR-261). The narrow ``admin`` role (Access admin) is not one."""
    from axiom.governance.roles import SITE_ADMIN, canonical_roles

    return SITE_ADMIN in canonical_roles(roles)


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
    if "no flag" in text or "nothing on this node" in text or "no change" in text or "no waiting change" in text:
        return 404
    if "no attribute" in text or "no cohort draft" in text or "no rollout draft" in text:
        return 404
    if "stale draft" in text:
        return 409
    if "protected" in text or "change's author" in text or "holds none of the roles" in text:
        return 403
    if "which is off on this node" in text or "is in use by" in text or "has already approved" in text:
        return 409
    if "nothing to approve" in text or "plan-matches-call" in text or "plan-unused" in text:
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
        "site_admin": is_site_admin(claims.get("roles")),
        # key -> the public message of every stop engaged here; never the internal reason (SW-A19).
        "stopped": _stopped(ctx),
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


def _stopped(ctx: Any) -> dict:
    from axiom.infra.skill_dispatch import WEB_SURFACE, invoke_capability

    out = invoke_capability(ctx.registry, "flags.cohorts", {}, ctx, surface=WEB_SURFACE)
    rollouts = (out.value or {}).get("rollouts") or [] if out.ok else []
    return {r["key"]: {"message": r.get("kill_message")} for r in rollouts if r.get("kill_switch")}


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
        message: str | None = None

    switches = subpath.rstrip("/").endswith("/features")

    @router.get(subpath + "/{key}/explain", tags=[tag])
    def explain_flag(key: str, request: Request, user: str = "", account: str = "") -> dict:
        claims = _claims(request)
        _signed_in(claims, HTTPException)
        params = {"key": key, **_who(claims, request)}
        own = {params["user"], ""}, {params["account"], ""}
        admin = is_site_admin(claims.get("roles"))
        if user not in own[0] or account not in own[1]:
            if not admin:
                raise HTTPException(403, "only a site admin explains a flag for somebody else")
            # Somebody else: what the server knows of them, not the site admin's own session.
            params = {"key": key, "user": user or params["user"], "account": account or params["account"]}
        ctx = _ctx(_actor(claims))
        result = invoke_capability(ctx.registry, "flags.explain", params, ctx, surface=WEB_SURFACE)
        if not result.ok:
            raise HTTPException(_status_for(result.errors), "; ".join(result.errors))
        # Scoped to the viewer (SW-A18): a person reads the sentence; only a site admin the trace.
        value = result.value
        body = {k: value.get(k) for k in ("flag", "value", "sentence", "ask")}
        if admin:
            body["trace"] = value
        return body

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
            {"key": key, "reason": body.reason, "engaged": body.engaged, "environment": body.environment or "",
             "message": body.message or ""},
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
        if key in POLICY_SKILLS:
            # A node policy (maintenance, record of truth, agents): its own verb, node scope.
            _check_scope(claims, "node", HTTPException)
            skill, param = POLICY_SKILLS[key]
            result = _call(claims, skill, {param: str(body.value or "").strip()})
            if not result.ok:
                raise HTTPException(_status_for(result.errors), "; ".join(result.errors))
            return {"set": result.value, **_resolved(claims, switches=True, request=request)}
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

    _screen_routes(router, subpath)


# ------------------------------------------------------------------ the feature-flag screens (site admin)


def _site_admin(request: Any, HTTPException, what: str) -> dict:
    claims = _claims(request)
    _signed_in(claims, HTTPException)
    if not is_site_admin(claims.get("roles")):
        raise HTTPException(403, f"only a site admin {what}")
    return claims


def _call(claims: dict, capability: str, params: dict, ctx: Any = None) -> Any:
    from axiom.infra.skill_dispatch import WEB_SURFACE, invoke_capability

    ctx = ctx or _ctx(_actor(claims))
    return invoke_capability(ctx.registry, capability, params, ctx, surface=WEB_SURFACE)


def _value(result: Any, HTTPException) -> Any:
    if not result.ok:
        raise HTTPException(_status_for(result.errors), "; ".join(result.errors))
    return result.value


def _given(**params: Any) -> dict:
    """The parameters a body named: one call previews and applies with the same ones, so the
    plan hash binds exactly what the person saw."""
    return {k: v for k, v in params.items() if v not in (None, "")}


def _open_plan(claims: dict, capability: str, params: dict, HTTPException) -> dict:
    """Open (or find) the plan for a destructive call, which the web surface holds until the
    person presses Apply: ``{plan_hash, held_action_id}``, and ``waiting`` when somebody else's
    change to the same cohort or flag waits for approval (Q16)."""
    ctx = _ctx(_actor(claims))
    result = _call(claims, capability, params, ctx)
    value = result.value if isinstance(result.value, dict) else {}
    if result.ok or not value.get("plan_hash"):
        raise HTTPException(500, "; ".join(result.errors) or f"{capability} opened no plan")
    out = {"plan_hash": value["plan_hash"], "held_action_id": value["held_action_id"]}
    kind = "cohort" if capability.startswith("flags.cohort") else "flag"
    listed = _call(claims, "flags.waiting", {"subject": f"{kind}:{params.get('key')}"}, ctx)
    from .approvals import _name

    others = [w for w in (listed.value or {}).get("waiting") or []
              if _name(w["author"]) != _name(_actor(claims))] if listed.ok else []
    if others:
        out["waiting"] = {k: others[0].get(k) for k in ("author", "author_name", "checked_at", "change_id")}
    return out


def _cohort_affected(preview: dict) -> dict:
    """The Check step's view of a cohort change (SW-A30): who it holds, who joins and leaves, and
    which flags read it."""
    members, joins, leaves = preview["members"], preview["joins"], preview["leaves"]
    sample = ([{"who": w, "change": "joins"} for w in joins["sample"]]
              + [{"who": w, "change": "leaves"} for w in leaves["sample"]])[:10]
    return {"count": members["count"], "unknown": members["unknown"], "population": preview["population"],
            "joins": joins["count"], "leaves": leaves["count"], "sites": preview.get("sites"), "sample": sample,
            "read_by": preview["read_by"]}


def _waiting(change: dict) -> str:
    need = max((int(p.get("approvals") or 0) for p in change.get("policy") or []), default=1)
    more = max(need - len(change.get("approvals") or []), 1)
    who = "another site admin" if more == 1 else f"{more} more people"
    return f"This change waits for {who} to approve it; nothing has changed yet."


#: The draft kind an apply may come from (dual drive): its card's Apply names the draft's revision.
_DRAFT_KIND = {"flags.cohort_set": "cohort", "flags.rollout_set": "rollout"}


def _drafted_via(claims: dict, capability: str, key: Any, draft_revision: int | None, ctx: Any) -> str | None:
    """``"chat"`` when this apply is of the person's own draft, at the revision they pressed Apply on,
    and chat last changed it (SW-A36); else ``None``, and the write is credited to the web."""
    kind = _DRAFT_KIND.get(capability)
    if kind is None or draft_revision is None:
        return None
    got = _call(claims, f"flags.{kind}_draft_get", {"key": key}, ctx)
    draft = (got.value or {}).get("draft") if got.ok else None
    if draft and draft["revision"] == draft_revision and draft["updated_via"] == "chat":
        return "chat"
    return None


def _apply_plan(claims: dict, capability: str, params: dict, plan_hash: str, HTTPException,
                draft_revision: int | None = None) -> Any:
    """Apply what the site admin previewed: the route approves the held plan as this person, through
    the gate's own approve, then runs the call with its hash (Change, then Apply). A hash that does
    not belong to these parameters, or names no open plan, is 409 and nothing applies. A change the
    site's policy holds for a second person answers 202 and waits for their approval (F23)."""
    from fastapi.responses import JSONResponse

    from axiom.infra.skill_chain import find_plan, plan_digest, plan_gate

    if plan_hash != plan_digest(capability, params):
        raise HTTPException(409, "this plan_hash does not belong to these parameters; preview the change again")
    ctx = _ctx(_actor(claims))
    action = find_plan(ctx, capability, plan_hash)
    if action is None:
        raise HTTPException(409, "no open plan has this plan_hash (it was applied or withdrawn); preview again")
    if action.status.value == "pending":
        plan_gate(ctx).approve(action.action_id, decided_by=_actor(claims))
    from axiom.infra import audit_trail

    with audit_trail.changes_via(_drafted_via(claims, capability, params.get("key"), draft_revision, ctx)):
        result = _call(claims, capability, {**params, "plan_hash": plan_hash}, ctx)
    value = result.value if isinstance(result.value, dict) else {}
    if result.ok:
        return {"applied": bool(value.get("applied")), "result": value}
    change = value.get("change")
    if change and any(u.get("id") == "approvals" for u in result.unmet or ()):
        return JSONResponse(status_code=202, content={
            "applied": False, "pending": {"change": change}, "waiting": _waiting(change)})
    raise HTTPException(_status_for(result.errors), "; ".join(result.errors))


def _screen_routes(router: Any, subpath: str) -> None:
    from fastapi import HTTPException, Request
    from pydantic import BaseModel

    class Timed(BaseModel):
        reason: str = ""
        environment: str | None = None
        at: str | None = None
        timezone: str | None = None
        plan_hash: str | None = None
        #: The draft revision a draft card's Apply names (dual drive), so the change is credited to chat.
        draft_revision: int | None = None

    class RolloutBody(Timed):
        rules: Any = None
        bucket_by: str | None = None

    class CohortBody(Timed):
        name: str | None = None
        description: str | None = None
        rules: Any = None
        principals: list[str] | None = None
        accounts: list[str] | None = None
        source: Any = None

    class ImportBody(Timed):
        members: list[str] | None = None
        file_text: str | None = None
        kind: str | None = None
        mode: str | None = None

    class DraftBody(BaseModel):
        body: Any = None
        revision: int | None = None

    class CountBody(BaseModel):
        rules: Any = None
        principals: list[str] | None = None
        accounts: list[str] | None = None

    class StageBody(Timed):
        plan: Any = None

    class ApproveBody(BaseModel):
        reason: str | None = None

    class StopBody(BaseModel):
        reason: str
        message: str | None = None
        environment: str | None = None

    class ReleaseBody(BaseModel):
        reason: str | None = None
        environment: str | None = None

    class ReasonBody(BaseModel):
        reason: str = ""

    class HaltBody(BaseModel):
        reason: str = ""
        environment: str | None = None
        mode: str | None = None

    # -------------------------------------------------------------- reads

    @router.get(subpath + "/admin", tags=["flags"])
    def admin_view(request: Request, environment: str = "") -> dict:
        claims = _site_admin(request, HTTPException, "sees every flag's rollout")
        who = _who(claims, request)
        ctx = _ctx(_actor(claims))
        listed = _value(_call(claims, "flags.list", {"user": who["user"], "account": who["account"]}, ctx),
                        HTTPException)
        targeting = _value(_call(claims, "flags.cohorts", _given(environment=environment), ctx), HTTPException)
        waiting = _value(_call(claims, "flags.changes", {"status": "awaiting_approval,scheduled"}, ctx),
                         HTTPException)
        resolved = {r["name"]: r for r in listed["rows"]}
        values = {k: r["value"] for k, r in resolved.items()}
        rollouts = {r["key"]: r for r in targeting["rollouts"]}
        plans = {p["key"]: p for p in targeting["stage_plans"]}
        rows = []
        for f in listed["flags"]:
            ro = rollouts.get(f["key"]) or {}
            row = resolved.get(f["key"]) or {}
            details = row.get("details") or {}
            rows.append({
                "key": f["key"], "description": f["description"], "owner": f["owner"],
                "protected": f["protected"], "value": values.get(f["key"]), "waiting_on": f["waiting_on"],
                # What the flag page shows, passed through from the declaration and this caller's resolve.
                "kind": f.get("kind"), "default": f.get("default"), "remove_by": f.get("remove_by"),
                "variants": sorted(f["variants"]) if f.get("variants") else None,
                "needs": [{"flag": n["flag"], "values": n["values"], "met": values.get(n["flag"]) in n["values"]}
                          for n in f.get("requires") or []],
                "source": row.get("source"),
                "staged": ({"value": details["staged_value"], "source": details.get("staged_source")}
                           if details.get("staged_value") is not None else None),
                "rollout": ({"rules": ro["rules"], "bucket_by": ro.get("bucket_by")}
                            if ro.get("rules") is not None else None),
                "stage_plan": plans.get(f["key"]),
                "stop": ({"by": ro.get("kill_by"), "at": ro.get("kill_at"), "reason": ro.get("kill_reason"),
                          "message": ro.get("kill_message")} if ro.get("kill_switch") else None),
                "overrides": f["overrides"],
            })
        # ``me`` is in the form a change's author and approvals[].by take, so a page tells the
        # author of a waiting change (withdraw) from another site admin (approve).
        return {"environment": targeting["environment"], "me": _actor(claims), "flags": rows,
                "changes": waiting["changes"]}

    @router.get(subpath + "/cohorts", tags=["flags"])
    def list_cohorts(request: Request, environment: str = "") -> dict:
        claims = _site_admin(request, HTTPException, "sees the cohorts")
        return _value(_call(claims, "flags.cohorts", _given(environment=environment)), HTTPException)

    @router.get(subpath + "/changes", tags=["flags"])
    def list_changes(request: Request, status: str = "") -> dict:
        claims = _site_admin(request, HTTPException, "sees the waiting changes")
        return _value(_call(claims, "flags.changes", _given(status=status)), HTTPException)

    # -------------------------------------------------------------- the rule builder (dual drive)

    @router.get(subpath + "/attributes", tags=["flags"])
    def list_attributes(request: Request) -> list:
        """What a rule may name, as the builder's menu shows it: ``[{name, label, group, description,
        available, has_values}]``."""
        claims = _claims(request)
        _signed_in(claims, HTTPException)
        return _value(_call(claims, "flags.attributes", {}), HTTPException)["attributes"]

    @router.get(subpath + "/values/{attribute}", tags=["flags"])
    def list_values(attribute: str, request: Request, q: str = "", limit: int = 50) -> dict:
        """The values to pick for one attribute (SW-A26): ``{attribute, source, values, more}``."""
        claims = _claims(request)
        _signed_in(claims, HTTPException)
        return _value(_call(claims, "flags.values", {"attribute": attribute, "query": q, "limit": limit}),
                      HTTPException)

    _KINDS = {"cohorts": "cohort", "rollouts": "rollout"}

    def _kind(kinds: str) -> str:
        if kinds not in _KINDS:
            raise HTTPException(404, "drafts are cohorts or rollouts")
        return _KINDS[kinds]

    @router.get(subpath + "/drafts/{kinds}/{key}", tags=["flags"])
    def get_draft(kinds: str, key: str, request: Request, since: int | None = None) -> Any:
        """The session's own draft; 304 when it is still at revision ``since``."""
        from fastapi.responses import Response

        claims = _site_admin(request, HTTPException, "keeps a draft")
        draft = _value(_call(claims, f"flags.{_kind(kinds)}_draft_get", {"key": key}), HTTPException)["draft"]
        if since is not None and draft["revision"] == since:
            return Response(status_code=304)
        return {"draft": draft}

    @router.put(subpath + "/drafts/{kinds}/{key}", tags=["flags"])
    def put_draft(kinds: str, key: str, request: Request, body: DraftBody) -> Any:
        """Create or save a new version of the session's own draft; a stale revision is 409 with
        the current draft."""
        from fastapi.responses import JSONResponse

        claims = _site_admin(request, HTTPException, "keeps a draft")
        kind = _kind(kinds)
        ctx = _ctx(_actor(claims))
        exists = _call(claims, f"flags.{kind}_draft_get", {"key": key}, ctx).ok
        params = {"key": key, "body": body.body, **_given(revision=body.revision)}
        result = _call(claims, f"flags.{kind}_draft_{'update' if exists else 'create'}", params, ctx)
        if not result.ok and isinstance(result.value, dict) and result.value.get("draft"):
            return JSONResponse(status_code=409, content={"detail": "; ".join(result.errors),
                                                          "draft": result.value["draft"]})
        return {"draft": _value(result, HTTPException)["draft"]}

    @router.delete(subpath + "/drafts/{kinds}/{key}", tags=["flags"])
    def delete_draft(kinds: str, key: str, request: Request) -> dict:
        claims = _site_admin(request, HTTPException, "keeps a draft")
        return _value(_call(claims, f"flags.{_kind(kinds)}_draft_discard", {"key": key}), HTTPException)

    @router.post(subpath + "/cohorts/{key}/count", tags=["flags"])
    def cohort_count(key: str, request: Request, body: CountBody) -> dict:
        """The builder's live count (SW-A29): who the rules as typed hold and why, over
        ``flags.cohort_preview``. Read-only: it opens no plan."""
        claims = _site_admin(request, HTTPException, "counts a cohort")
        params = _given(key=key, rules=body.rules, principals=body.principals, accounts=body.accounts)
        preview = _value(_call(claims, "flags.cohort_preview", params), HTTPException)
        members = preview["members"]
        unknown = ({"count": members["unknown"], "why": members.get("unknown_why")}
                   if members["unknown"] else None)
        return {"count": members["count"], "sites": preview.get("sites"), "sample": members.get("why") or [],
                "unknown": unknown, "population": preview["population"]}

    # -------------------------------------------------------------- cohorts (before /{key}/..., so
    # a cohort named like a route word still reaches its own route)

    def _cohort_params(key: str, body: CohortBody) -> dict:
        return _given(key=key, name=body.name, description=body.description, rules=body.rules,
                      principals=body.principals, accounts=body.accounts, source=body.source,
                      reason=body.reason, at=body.at, timezone=body.timezone)

    @router.post(subpath + "/cohorts/{key}/preview", tags=["flags"])
    def cohort_preview(key: str, request: Request, body: CohortBody) -> dict:
        claims = _site_admin(request, HTTPException, "changes a cohort")
        params = _cohort_params(key, body)
        preview = _value(_call(claims, "flags.cohort_preview", params), HTTPException)
        return {"preview": preview, **_open_plan(claims, "flags.cohort_set", params, HTTPException),
                "affected": _cohort_affected(preview),
                "approvals_needed": preview["approvals_needed"]}

    @router.post(subpath + "/cohorts/{key}/apply", tags=["flags"])
    def cohort_apply(key: str, request: Request, body: CohortBody) -> Any:
        claims = _site_admin(request, HTTPException, "changes a cohort")
        params = _cohort_params(key, body)
        return _apply_plan(claims, "flags.cohort_set", params, body.plan_hash or "", HTTPException,
                           draft_revision=body.draft_revision)

    def _import_params(key: str, body: ImportBody) -> dict:
        return _given(key=key, members=body.members, file_text=body.file_text, kind=body.kind, mode=body.mode,
                      reason=body.reason, at=body.at, timezone=body.timezone)

    @router.post(subpath + "/cohorts/{key}/import/preview", tags=["flags"])
    def import_preview(key: str, request: Request, body: ImportBody) -> dict:
        claims = _site_admin(request, HTTPException, "imports a cohort")
        params = _import_params(key, body)
        preview = _value(_call(claims, "flags.cohort_import_preview", params), HTTPException)
        return {"preview": preview, "joins": preview["added"], "leaves": preview["removed"],
                "unknown": preview["unknown"], **_open_plan(claims, "flags.cohort_import", params, HTTPException),
                "approvals_needed": preview["approvals_needed"]}

    @router.post(subpath + "/cohorts/{key}/import/apply", tags=["flags"])
    def import_apply(key: str, request: Request, body: ImportBody) -> Any:
        claims = _site_admin(request, HTTPException, "imports a cohort")
        params = _import_params(key, body)
        return _apply_plan(claims, "flags.cohort_import", params, body.plan_hash or "", HTTPException)

    # -------------------------------------------------------------- changes: approve, withdraw

    def _approve_change(change_id: str, request: Request, body: ApproveBody | None = None) -> dict:
        """A second site admin approves a waiting change (F23, an ordinary approval through the ask
        path, ADR-253); when that completes its approvals it applies, now or at its time. The author
        never approves their own change."""
        claims = _site_admin(request, HTTPException, "approves a change")
        params = {"id": change_id, "apply": True, **_given(reason=body.reason if body else None)}
        value = _value(_call(claims, "flags.approve", params), HTTPException)
        return {"change": value["change"], "applied": bool(value.get("applied"))}

    router.post(subpath + "/changes/{change_id}/approve", tags=["flags"])(_approve_change)
    # The round-1 name, kept as an alias.
    router.post(subpath + "/changes/{change_id}/cosign", tags=["flags"])(_approve_change)

    @router.get(subpath + "/waiting", tags=["flags"])
    def waiting(request: Request, subject: str = "") -> dict:
        """Changes waiting for a second person's approval, for one cohort or flag
        (``subject=cohort:<key>`` or ``flag:<key>``): ``{waiting: [{kind, key, author, author_name,
        checked_at, change_id}]}`` (Q16). Every site admin of the site sees them."""
        claims = _site_admin(request, HTTPException, "sees the changes waiting for approval")
        return _value(_call(claims, "flags.waiting", _given(subject=subject)), HTTPException)

    @router.post(subpath + "/waiting/{change_id}/withdraw", tags=["flags"])
    def withdraw_waiting(change_id: str, request: Request, body: ReasonBody | None = None) -> dict:
        """The author withdraws their own waiting change (Q16)."""
        from .approvals import _name

        claims = _site_admin(request, HTTPException, "withdraws a change")
        listed = _value(_call(claims, "flags.changes", {}), HTTPException)
        change = next((c for c in listed["changes"] if c["id"] == change_id), None)
        if change is None:
            raise HTTPException(404, f"no change {change_id!r}")
        if _name(change["author"]) != _name(_actor(claims)):
            raise HTTPException(403, "only the change's author withdraws it here")
        reason = (body.reason if body else "") or "withdrawn by its author"
        return _value(_call(claims, "flags.change_cancel", {"id": change_id, "reason": reason}), HTTPException)

    @router.post(subpath + "/changes/{change_id}/withdraw", tags=["flags"])
    def withdraw(change_id: str, request: Request, body: ReasonBody) -> dict:
        from .approvals import _name

        claims = _claims(request)
        _signed_in(claims, HTTPException)
        listed = _value(_call(claims, "flags.changes", {}), HTTPException)
        change = next((c for c in listed["changes"] if c["id"] == change_id), None)
        if change is None:
            raise HTTPException(404, f"no change {change_id!r}")
        if not is_site_admin(claims.get("roles")) and _name(change["author"]) != _name(_actor(claims)):
            raise HTTPException(403, "only the change's author or a site admin withdraws it")
        return _value(_call(claims, "flags.change_cancel", {"id": change_id, "reason": body.reason}), HTTPException)

    # -------------------------------------------------------------- one flag: stop, rollout, stages

    @router.post(subpath + "/{key}/stop", tags=["flags"])
    def stop(key: str, request: Request, body: StopBody) -> dict:
        claims = _site_admin(request, HTTPException, "stops a flag")
        params = {"key": key, "engaged": True, "reason": body.reason, **_given(message=body.message,
                                                                              environment=body.environment)}
        value = _value(_call(claims, "flags.kill", params), HTTPException)
        return {"stop": value, **_resolved(claims, switches=False, request=request)}

    @router.post(subpath + "/{key}/release", tags=["flags"])
    def release(key: str, request: Request, body: ReleaseBody) -> dict:
        claims = _site_admin(request, HTTPException, "releases a stopped flag")
        params = {"key": key, "engaged": False, **_given(reason=body.reason, environment=body.environment)}
        value = _value(_call(claims, "flags.kill", params), HTTPException)
        return {"stop": value, **_resolved(claims, switches=False, request=request)}

    def _rollout_params(key: str, body: RolloutBody) -> dict:
        return _given(key=key, rules=body.rules, environment=body.environment, bucket_by=body.bucket_by,
                      reason=body.reason, at=body.at, timezone=body.timezone)

    @router.post(subpath + "/{key}/rollout/preview", tags=["flags"])
    def rollout_preview(key: str, request: Request, body: RolloutBody) -> dict:
        claims = _site_admin(request, HTTPException, "changes a rollout")
        params = _rollout_params(key, body)
        preview = _value(_call(claims, "flags.rollout_preview", params), HTTPException)
        return {"preview": preview, **_open_plan(claims, "flags.rollout_set", params, HTTPException),
                "affected": {"count": preview["changes"]["count"], "sample": preview["changes"]["sample"],
                             "per_rule": preview["per_rule"], "population": preview["population"]},
                "approvals_needed": preview["approvals_needed"]}

    @router.post(subpath + "/{key}/rollout/apply", tags=["flags"])
    def rollout_apply(key: str, request: Request, body: RolloutBody) -> Any:
        claims = _site_admin(request, HTTPException, "changes a rollout")
        params = _rollout_params(key, body)
        return _apply_plan(claims, "flags.rollout_set", params, body.plan_hash or "", HTTPException,
                           draft_revision=body.draft_revision)

    def _stage_params(key: str, body: StageBody) -> dict:
        return _given(key=key, plan=body.plan, environment=body.environment, reason=body.reason, at=body.at,
                      timezone=body.timezone)

    @router.post(subpath + "/{key}/stages/preview", tags=["flags"])
    def stages_preview(key: str, request: Request, body: StageBody) -> dict:
        claims = _site_admin(request, HTTPException, "changes a staged rollout")
        params = _stage_params(key, body)
        preview = _value(_call(claims, "flags.stage_plan_preview", params), HTTPException)
        return {"preview": preview, **_open_plan(claims, "flags.stage_plan_set", params, HTTPException),
                "approvals_needed": preview["approvals_needed"]}

    @router.post(subpath + "/{key}/stages/apply", tags=["flags"])
    def stages_apply(key: str, request: Request, body: StageBody) -> Any:
        claims = _site_admin(request, HTTPException, "changes a staged rollout")
        params = _stage_params(key, body)
        return _apply_plan(claims, "flags.stage_plan_set", params, body.plan_hash or "", HTTPException)

    @router.post(subpath + "/{key}/stages/halt", tags=["flags"])
    def halt_stages(key: str, request: Request, body: HaltBody) -> dict:
        claims = _site_admin(request, HTTPException, "halts a staged rollout")
        params = {"key": key, "reason": body.reason, **_given(environment=body.environment, mode=body.mode)}
        return {"halted": _value(_call(claims, "flags.stage_plan_halt", params), HTTPException)}


__all__ = [
    "ACCOUNT_ROLES",
    "NODE_ROLES",
    "is_site_admin",
    "may_set",
    "register_flag_routes",
    "register_routes",
]
