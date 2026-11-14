# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/api/v1/settings`` — the web Settings panel's face over the settings skills.

ALLOWLISTED, never the whole store. The store holds values that can carry a
credential and switches that govern safety, so the browser sees only the keys
named here, and can change fewer still:

- ``WEB_READABLE``: shown in the System section to anyone signed in.
- ``WEB_EDITABLE``: changeable from the web, by the owner or a site admin only.
  Nothing on it can lower a safety or export-control posture: autonomy, trust
  and routing sensitivity stay readable here and change at the node's CLI.

No ``from __future__ import annotations``: FastAPI resolves handler
annotations at registration and ``Request`` is imported inside
``register_routes``.
"""

import logging
from typing import Any

WEB_READABLE: tuple[str, ...] = (
    "interface.stream",
    "chat.advisor.enabled",
    "routing.default_mode",
    "routing.cloud_provider",
    "routing.prefer_provider",
    "routing.sensitivity",
    "raci.trust",
    "raci.preset",
    "autonomy.enabled",
)

WEB_EDITABLE: frozenset[str] = frozenset({"interface.stream", "chat.advisor.enabled"})

EDIT_ROLES = frozenset({"owner", "site-admin", "admin"})


def may_edit(roles: Any) -> bool:
    """Whether a session holding ``roles`` changes node settings from the web.

    Read through ``canonical_roles``: a grant stored as ``operator`` is ``site-admin`` (ADR-261).
    """
    from axiom.governance.roles import canonical_roles

    return bool(EDIT_ROLES & set(canonical_roles(roles or ())))

#: What the System section says beside each key. Short, and in the reader's terms.
DESCRIPTIONS: dict[str, str] = {
    "interface.stream": "Show answers as they are written, rather than all at once",
    "chat.advisor.enabled": "Let the assistant suggest a better model for a question",
    "routing.default_mode": "How questions are routed to a model",
    "routing.cloud_provider": "The cloud provider used when routing allows it",
    "routing.prefer_provider": "Providers tried first, in order",
    "routing.sensitivity": "How readily content is treated as export-controlled",
    "raci.trust": "How readily actions are approved without asking (1 to 5)",
    "raci.preset": "Review preset for pull-request landing (strict, balanced, or autonomous)",
    "autonomy.enabled": "Whether agents run on their own at all",
}


def _cli() -> str:
    from axiom.infra.branding import get_branding

    return get_branding().cli_name


def _claims(request: Any) -> dict:
    try:
        from axiom.webauth import session_from_cookies

        return session_from_cookies(dict(request.cookies)) or {}
    except Exception:  # noqa: BLE001 - no session is "not signed in"
        return {}


def principal_of(claims: dict) -> str | None:
    """The signed-in person as the change log names them: the gate's own rule."""
    if not claims:
        return None
    try:
        from axiom.extensions.builtins.webgate.api.self_key_route import _principal_of

        return _principal_of(claims) or None
    except Exception:  # noqa: BLE001 - an unnamed caller is recorded as the default
        return None


def _ctx(claims: dict | None = None):
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skills import SkillContext, SkillRegistry

    from .skills import bind

    registry = SkillRegistry()
    bind(registry)
    # The person, not the node's CLI default: the change log's "who" (MVP-08-A2).
    return SkillContext(
        registry=registry, state_dir=get_user_state_dir(), logger=logging.getLogger("axi.settings"),
        actor=principal_of(claims or {}),
    )


def visible_rows(rows: list[dict], *, can_edit: bool) -> list[dict]:
    """The allowlisted rows, in allowlist order, each saying whether THIS viewer
    may change it here and, if not, where it is changed."""
    by_key = {r["key"]: r for r in rows}
    out = []
    for key in WEB_READABLE:
        if key not in by_key:
            continue
        row = dict(by_key[key])
        row["description"] = DESCRIPTIONS.get(key, "")
        row["editable"] = can_edit and key in WEB_EDITABLE
        if key not in WEB_EDITABLE:
            row["changed_at"] = f"{_cli()} settings set {key} <value>"
        out.append(row)
    return out


def register_routes(router: Any, *, subpath: str = "/settings") -> None:
    from fastapi import HTTPException, Request
    from pydantic import BaseModel

    from axiom.infra.skill_dispatch import WEB_SURFACE, invoke_capability

    class PatchBody(BaseModel):
        key: str
        value: Any

    @router.get(subpath, tags=["settings"])
    def read_settings(request: Request) -> dict:
        claims = _claims(request)
        if not claims:
            raise HTTPException(401, "sign in first")
        can_edit = may_edit(claims.get("roles"))
        # Through the dispatch chokepoint, never around it: authority gate,
        # audit and telemetry apply to a browser exactly as to the CLI.
        ctx = _ctx()
        result = invoke_capability(ctx.registry, "settings.list", {}, ctx, surface=WEB_SURFACE)
        if not result.ok:
            raise HTTPException(500, "; ".join(result.errors))
        return {
            "settings": visible_rows(result.value["settings"], can_edit=can_edit),
            "can_edit": can_edit,
        }

    @router.patch(subpath, tags=["settings"])
    def change_setting(request: Request, body: PatchBody) -> dict:
        claims = _claims(request)
        if not claims:
            raise HTTPException(401, "sign in first")
        if not may_edit(claims.get("roles")):
            raise HTTPException(403, "only the owner or a site admin changes this node's settings")
        if body.key not in WEB_EDITABLE:
            raise HTTPException(
                403,
                f"{body.key} is not changed from the web; use `{_cli()} settings set {body.key} <value>`",
            )
        ctx = _ctx(claims)
        result = invoke_capability(
            ctx.registry,
            "settings.set",
            {"key": body.key, "value": body.value, "scope": "global"},
            ctx,
            surface=WEB_SURFACE,
        )
        if not result.ok:
            raise HTTPException(400, "; ".join(result.errors))
        return result.value

    @router.get(subpath + "/pages", tags=["settings"])
    def read_pages(request: Request) -> dict:
        """Account and Settings sections for this caller's roles (MVP-08-A1)."""
        from .pages import sections_for

        claims = _claims(request)
        if not claims:
            raise HTTPException(401, "sign in first")
        from axiom.governance.roles import canonical_roles, display_name

        roles = list(canonical_roles(claims.get("roles") or ()))
        return {**sections_for(roles), "roles": roles, "role_names": [display_name(r) for r in roles]}

    @router.get(subpath + "/changes", tags=["settings"])
    def read_changes(request: Request, mine: bool = False, kind: str | None = None,
                     limit: int = 50, audit: bool = False) -> dict:
        """The change log. Administrators read the site's; anyone reads their own (``mine``)."""
        claims = _claims(request)
        if not claims:
            raise HTTPException(401, "sign in first")
        is_admin = may_edit(claims.get("roles"))
        if not mine and not is_admin:
            raise HTTPException(403, "the site's change log is for administrators; your own is under Account › Data")
        if audit and not is_admin:
            raise HTTPException(403, "the audit trail is for administrators")
        ctx = _ctx(claims)
        params: dict = {"limit": limit, "mine": bool(mine), "audit": bool(audit)}
        if kind:
            params["kind"] = kind
        result = invoke_capability(ctx.registry, "settings.changes", params, ctx, surface=WEB_SURFACE)
        if not result.ok:
            raise HTTPException(400, "; ".join(result.errors))
        return result.value
