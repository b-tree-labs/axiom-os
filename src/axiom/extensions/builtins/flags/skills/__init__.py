# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``flags.list`` / ``flags.resolve`` / ``flags.set``: feature flags on every surface (ADR-056, ADR-210).

One implementation; the CLI (``axi flags``), MCP (``axiom_flags__*``) and HTTP
(``/api/v1/flags``) all reach it through ``invoke_capability``, so a value set
on one surface resolves the same on the others, and every ``flags.set`` lands
in the action audit (``axi audit actions``) because the skill is mutating.

Who may set which scope is the surface's call: the node's own CLI is its
operator; the HTTP face lets a signed-in person set only their own user
override and leaves account and node scope to owners and operators.
"""

from __future__ import annotations

import os
from typing import Any

from axiom.infra.skill_declarations import NextStep
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec, default_registry

#: The word ``flags.set`` takes to remove an override (back to the layer below).
CLEAR = "clear"


def this_node() -> str:
    """How this node names itself (``AXIOM_NODE_NAME``, else the short hostname)."""
    from axiom.infra.topology import this_node as _this_node

    return _this_node()["node"]


def default_account() -> str | None:
    """The tenancy a caller who names no account resolves under (``AXIOM_SITE``)."""
    return os.environ.get("AXIOM_SITE") or None


def _cli() -> str:
    from axiom.infra.branding import get_branding

    return get_branding().cli_name


def _service():
    from ..service import FlagService

    return FlagService()


def _who(params: dict[str, Any]) -> dict[str, str | None]:
    return {
        "node": str(params.get("node") or "") or this_node(),
        "account": str(params.get("account") or "") or default_account(),
        "user": str(params.get("user") or "") or None,
    }


def list_flags(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    svc = _service()
    prefix = str(params.get("prefix") or "").strip()
    from ..registry import get_environment

    meta = svc.get_flag_metadata()
    overrides = svc.overrides.rows() if hasattr(svc.overrides, "rows") else []
    by_key: dict[str, list[dict[str, Any]]] = {}
    for row in overrides:
        by_key.setdefault(row["key"], []).append(row)
    rows = [
        {"key": key, **info, "overrides": by_key.get(key, [])}
        for key, info in meta.items()
        if not prefix or key.startswith(prefix)
    ]
    return SkillResult(value={"environment": get_environment(), "flags": rows})


def resolve_flags(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    who = _who(params)
    res = _service().resolve(**who)
    key = str(params.get("key") or "").strip()
    flags, sources = res.flags, res.sources
    if key:
        flags = {key: flags.get(key, "off")}
        sources = {key: sources.get(key, "undeclared")}
    return SkillResult(
        value={"environment": res.environment, "for": who, "flags": flags, "sources": sources}
    )


def set_flag(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from ..service import FlagError

    key = str(params.get("key") or "").strip()
    if not key:
        return SkillResult(ok=False, errors=["name the flag: key is required"])
    if "value" not in params or params.get("value") in (None, ""):
        return SkillResult(ok=False, errors=[f"give the value: on, off, soon, or {CLEAR}"])
    scope = str(params.get("scope") or "node").strip().lower()
    scope_id = str(params.get("scope_id") or "").strip()
    if not scope_id and scope == "node":
        scope_id = this_node()
    if not scope_id and scope == "account":
        scope_id = default_account() or ""
    raw = params["value"]
    value = None if str(raw).strip().lower() == CLEAR else raw
    from axiom.infra.audit_trail import DEFAULT_PRINCIPAL

    by = getattr(ctx, "actor", None) or DEFAULT_PRINCIPAL
    try:
        row = _service().set_override(scope, scope_id, key, value, by=by)
    except FlagError as exc:
        hint = f"; `{_cli()} flags list` names the declared flags" if exc.code == "unknown" else ""
        return SkillResult(ok=False, errors=[f"{exc}{hint}"])
    return SkillResult(
        value={
            "key": key,
            "scope": scope,
            "scope_id": scope_id,
            "value": row["value"] if row else None,
            "cleared": row is None,
            "updated_by": by,
        },
        actions_taken=[
            f"cleared the {scope} override of {key} for {scope_id}"
            if row is None
            else f"set {key}={row['value']} for {scope} {scope_id}"
        ],
    )


#: Chaining declarations (ADR-185): every skill declares its risk, and a write
#: what has to hold first (nothing: a flag override is set back with `clear`).
_CHAINING: dict[str, dict] = {
    "flags.list": dict(
        risk="read",
        next_steps=(NextStep("flags.set", "always", "override a listed flag for a node, account or user"),),
    ),
    "flags.resolve": dict(risk="read"),
    "flags.set": dict(
        risk="reversible-write",
        requires=(),
        next_steps=(NextStep("flags.resolve", "ok", "confirm what the caller now resolves"),),
    ),
}

_SURFACES = ("cli", "mcp", "agent_tool", "skill_md")

_SPECS = (
    (
        "flags.list",
        list_flags,
        "List the declared feature flags: each one's default in this environment, whether it is "
        "protected here, the extension that declares it, and its stored overrides. Read-only.",
        {"prefix": "optional key prefix"},
        False,
    ),
    (
        "flags.resolve",
        resolve_flags,
        "Resolve feature flags (on, off or soon) for a node, account and user, with where each "
        "value came from (default, env, node, account, user). Read-only.",
        {
            "key": "optional: resolve one flag",
            "node": "optional node name (default: this node)",
            "account": "optional account (default: $AXIOM_SITE)",
            "user": "optional user id",
        },
        False,
    ),
    (
        "flags.set",
        set_flag,
        "Override one feature flag for a node, account or user: value on, off, soon, or clear to "
        "remove the override. Refused for an undeclared flag or one protected in this environment.",
        {
            "key": "the flag key (required)",
            "value": "on | off | soon | clear (true/false accepted)",
            "scope": "node (default) | account | user",
            "scope_id": "the node, account or user id (default: this node, or $AXIOM_SITE)",
        },
        True,
    ),
)


def bind(registry: SkillRegistry) -> list[str]:
    names = []
    for name, fn, description, inputs, mutating in _SPECS:
        if not registry.has(name):
            registry.register_skill(
                SkillSpec(
                    name=name,
                    fn=fn,
                    description=description,
                    inputs=inputs,
                    side_effects=mutating,
                    idempotent=True,
                    surfaces=_SURFACES,
                    **_CHAINING[name],
                ),
                mutating=mutating,
            )
        names.append(name)
    return names


register = bind


def bind_default() -> SkillRegistry:
    registry = default_registry()
    bind(registry)
    return registry


__all__ = ["CLEAR", "bind", "bind_default", "default_account", "register", "this_node"]
