# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The flag verbs: list, resolve, set (ADR-210, ADR-254, amended 2026-11-12).

One implementation behind ``flags.list`` / ``flags.resolve`` / ``flags.set``
(and the deprecated ``features.resolve``); the CLI, MCP and HTTP reach it
through ``invoke_capability``, so a value set on one surface resolves the same
on the others and every write lands in the action audit.
"""

from __future__ import annotations

import os
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

#: The word ``set`` takes to remove an override (back to the layer below).
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
    """The flag resolver; a node whose role needs no database reads no store (LOOP-F13)."""
    from ..resolver import FlagService, NoOverrides

    try:
        from axiom.infra import node_functions as nf

        if not nf.needs_database(nf.load()):
            return FlagService(overrides=NoOverrides())
    except Exception:  # noqa: BLE001 - an unreadable node file is not a reason to skip the store
        pass
    return FlagService()


def who(params: dict[str, Any]) -> dict[str, str | None]:
    return {
        "node": str(params.get("node") or "") or this_node(),
        "account": str(params.get("account") or "") or default_account(),
        "user": str(params.get("user") or "") or None,
    }


def flag_metadata(svc=None) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """The declared flags' metadata, and their stored overrides grouped by key."""
    svc = svc or _service()
    meta = svc.get_flag_metadata()
    try:
        overrides = svc.overrides.rows() if hasattr(svc.overrides, "rows") else []
    except Exception:  # noqa: BLE001 - no store means no stored overrides to list; the declarations still show
        overrides = []
    by_key: dict[str, list[dict[str, Any]]] = {}
    for row in overrides:
        by_key.setdefault(row["key"], []).append(row)
    return meta, by_key


def list_flags(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``flags.list``: each declared flag with its value for the caller, default, owner and overrides.

    ``flags`` is the shape ``axiom_flags__list`` has always returned; ``rows`` are the same
    flags as rows of the one registry (kind ``flag``), resolved for ``user`` and ``account``.
    """
    from ..flag_registry import get_environment
    from ..registry import flag_rows

    prefix = str(params.get("prefix") or "").strip()
    svc = _service()
    meta, by_key = flag_metadata(svc)
    flags = [
        {"key": key, **info, "overrides": by_key.get(key, [])}
        for key, info in meta.items()
        if not prefix or key.startswith(prefix)
    ]
    w = who(params)
    problem = ""
    try:
        resolution = svc.resolve(node=w["node"], account=w["account"], user=w["user"])
    except Exception as exc:  # noqa: BLE001 - no store: the declarations still list
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        problem = f"flag values could not be resolved, so only the declarations are listed ({first})"
        rows = []
        environment = get_environment()
    else:
        rows = [r.as_dict() for r in flag_rows(resolution, meta, by_key, prefix=prefix)]
        environment = resolution.environment or get_environment()
    return SkillResult(value={"environment": environment, "flags": flags, "rows": rows, "problem": problem})


def resolve_flags(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from ._loop import who as subject

    full = subject(params)
    w = {k: full[k] for k in ("node", "account", "user")}
    res = _service().resolve(**full)
    key = str(params.get("key") or "").strip()
    flags, sources, values = res.flags, res.sources, res.values
    if key:
        flags = {key: flags.get(key, "off")}
        sources = {key: sources.get(key, "undeclared")}
        values = {k: v for k, v in values.items() if k == key}
    # A flag held by an off extension waits on it; ``staged`` is what applies once it is on.
    waiting = {k: v for k, v in res.waiting_on.items() if k in flags}
    staged = {k: {"value": v, "source": s} for k, (v, s) in res.staged.items() if k in flags}
    return SkillResult(
        value={"environment": res.environment, "for": w, "flags": flags, "sources": sources,
               # A multivariate flag's variant value (LOOP-F17); on/off/soon flags have none.
               "values": values,
               "waiting_on": waiting, "staged": staged,
               # "" when cohorts, rollouts and kill switches were read; else why not (LOOP-F13).
               "targeting": res.targeting}
    )


def set_flag(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from ..resolver import FlagError

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
    from ..person_only import refusal

    refused = refusal(key, f"set {key} to {raw} for {scope} {scope_id}", ctx)
    if refused:
        return SkillResult(ok=False, errors=[refused])
    from axiom.infra.audit_trail import DEFAULT_PRINCIPAL

    by = getattr(ctx, "actor", None) or DEFAULT_PRINCIPAL
    service = _service()
    try:
        before = (service.overrides.overrides_for(scope, service._sid(scope_id) or scope_id) or {}).get(key)
    except Exception:  # noqa: BLE001 - the old value is for the change log only
        before = None
    reason = str(params.get("reason") or "").strip() or None
    expires_at = str(params.get("expires_at") or "").strip() or None
    try:
        row = service.set_override(scope, scope_id, key, value, by=by, reason=reason, expires_at=expires_at)
    except FlagError as exc:
        hint = ""
        if exc.code == "unknown":
            hint = f"; `{_cli()} flags list` names the declared flags"
        return SkillResult(ok=False, errors=[f"{exc}{hint}"])
    from ..resolver import waiting_text

    waiting_on = service.waiting_on(key)
    from axiom.infra.audit_trail import note_change

    note_change(kind="flag", subject=f"{key} ({scope} {scope_id})", old=before,
                new=row["value"] if row else None)
    return SkillResult(
        value={
            "key": key,
            "kind": "flag",
            "scope": scope,
            "scope_id": scope_id,
            "value": row["value"] if row else None,
            "cleared": row is None,
            "updated_by": by,
            "reason": row.get("reason") if row else None,
            "expires_at": row.get("expires_at") if row else None,
            "waiting_on": waiting_on,
        },
        actions_taken=[
            (
                f"cleared the {scope} override of {key} for {scope_id}"
                if row is None
                else f"set {key}={row['value']} for {scope} {scope_id}"
            )
            + (
                f"; {waiting_text(waiting_on)}: it resolves off until "
                f"`{_cli()} features enable {waiting_on}`, then this value applies at once"
                if waiting_on and row is not None
                else ""
            )
        ],
    )


__all__ = [
    "CLEAR",
    "default_account",
    "flag_metadata",
    "list_flags",
    "resolve_flags",
    "set_flag",
    "this_node",
    "who",
]
