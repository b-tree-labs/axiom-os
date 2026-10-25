# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/api/v1/settings`` — the web Settings panel's face over the settings skills.

ALLOWLISTED, never the whole store. The store holds values that can carry a
credential and switches that govern safety, so the browser sees only the keys
named here, and can change fewer still:

- ``WEB_READABLE``: shown in the System section to anyone signed in.
- ``WEB_EDITABLE``: changeable from the web, by the owner or an operator only.
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
    "autonomy.enabled",
)

WEB_EDITABLE: frozenset[str] = frozenset({"interface.stream", "chat.advisor.enabled"})

EDIT_ROLES = frozenset({"owner", "operator", "admin"})

#: What the System section says beside each key. Short, and in the reader's terms.
DESCRIPTIONS: dict[str, str] = {
    "interface.stream": "Show answers as they are written, rather than all at once",
    "chat.advisor.enabled": "Let the assistant suggest a better model for a question",
    "routing.default_mode": "How questions are routed to a model",
    "routing.cloud_provider": "The cloud provider used when routing allows it",
    "routing.prefer_provider": "Providers tried first, in order",
    "routing.sensitivity": "How readily content is treated as export-controlled",
    "raci.trust": "How readily actions are approved without asking (1 to 5)",
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


def _ctx():
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skills import SkillContext, SkillRegistry

    from .skills import bind

    registry = SkillRegistry()
    bind(registry)
    return SkillContext(
        registry=registry, state_dir=get_user_state_dir(), logger=logging.getLogger("axi.settings")
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
        can_edit = bool(EDIT_ROLES & set(claims.get("roles") or []))
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
        if not EDIT_ROLES & set(claims.get("roles") or []):
            raise HTTPException(403, "only the owner or an operator changes this node's settings")
        if body.key not in WEB_EDITABLE:
            raise HTTPException(
                403,
                f"{body.key} is not changed from the web; use `{_cli()} settings set {body.key} <value>`",
            )
        ctx = _ctx()
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
