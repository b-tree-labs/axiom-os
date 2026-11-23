# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``grant.list``, ``grant.show``, ``grant.revoke`` and KEEP's grant-expiry sweep (RW-G1).

The skills behind ``axi grant`` (ADR-056). Each reads the one projection,
:mod:`axiom.governance.grants`, over KEEP's three stores; none holds a grant of its own.

Who the caller is comes from the context the dispatcher stamped, never from params: the
handle is ``ctx.principal``, the surface is ``ctx.surface``, and the caller is the owner when
its credential is not held to a site (:func:`axiom.infra.site_authority.tenant_scope`).
``grant.list`` and ``grant.show`` are reads on every surface; ``grant.revoke`` is projected on
the CLI only, and the projection refuses a ``sign`` or ``read`` revoke that arrived anywhere else.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from axiom.governance import grants as G
from axiom.infra.skill_declarations import NextStep, Precondition, Ref
from axiom.infra.skills import SkillContext, SkillResult, SkillSpec

#: KEEP's horizon for a grant that is about to lapse: one day, wide against the hourly heartbeat.
EXPIRY_HORIZON = timedelta(days=1)

_READ = ("cli", "mcp", "agent_tool")


def _caller(ctx: SkillContext | None) -> G.Caller:
    from axiom.infra.site_authority import caller_principal, tenant_scope

    try:
        owner = tenant_scope() is None
    except Exception:  # noqa: BLE001 - a credential that reads nothing owns nothing
        owner = False
    # The request's verified credential first (a served call); else the context's principal.
    handle = caller_principal() or getattr(getattr(ctx, "principal", None), "handle", "") or ""
    return G.Caller(handle=handle, surface=getattr(ctx, "surface", None), owner=owner)


def _sources(params: dict[str, Any]):
    return G.default_sources({"keys_file": params.get("keys_file")} if params.get("keys_file") else {})


def list_(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """Every live grant the caller may see, in one shape, earliest expiry first."""
    try:
        rows = G.list_grants(
            caller=_caller(ctx),
            principal=params.get("principal") or None,
            kind=params.get("kind") or None,
            sources=_sources(params),
        )
    except (ValueError, G.GrantRefused) as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(ok=True, value={"grants": [g.as_dict() for g in rows], "count": len(rows)})


def show(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    grant_id = str(params.get("id") or "").strip()
    if not grant_id:
        return SkillResult(ok=False, errors=["missing required argument: id (`grant list` shows them)"])
    try:
        grant = G.show(grant_id, caller=_caller(ctx), sources=_sources(params))
    except (G.GrantNotFound, G.GrantRefused) as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    return SkillResult(ok=True, value={"grant": grant.as_dict()})


def revoke(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    grant_id = str(params.get("id") or "").strip()
    if not grant_id:
        return SkillResult(ok=False, errors=["missing required argument: id (`grant list` shows them)"])
    caller = _caller(ctx)
    try:
        receipt = G.revoke(grant_id, caller=caller, reason=str(params.get("reason") or ""),
                           sources=_sources(params))  # fmt: skip
    except G.GrantRefused as exc:
        msg = str(exc)
        if exc.code == "cli_only":
            msg = f"{msg}. Run it yourself at the CLI."
        return SkillResult(ok=False, errors=[msg])
    except G.GrantNotFound as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    from axiom.infra.audit_trail import note_change

    note_change(kind="grant", subject=f"{receipt['id']} ({receipt['kind']}, {receipt['principal']})",
                old="active", new="revoked")  # fmt: skip
    return SkillResult(
        ok=True,
        value=receipt,
        actions_taken=[
            f"revoked {receipt['kind']} grant {receipt['id']} ({receipt['principal']}) in "
            f"{receipt['store']}; its next use is refused"
        ],
    )


def expiry(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """KEEP's grant-expiry pass (ADR-209 D8a): one read of the projection, every kind.

    Reports the earliest expiry across kinds, the earliest per kind, and every grant lapsing
    within the horizon. KEEP is the steward of every grant, so it reads as the owner."""
    try:
        hours = float(params.get("within_hours") or EXPIRY_HORIZON.total_seconds() / 3600)
    except (TypeError, ValueError):
        return SkillResult(ok=False, errors=["within_hours must be a number"])
    now = datetime.now(UTC)
    keep = G.Caller(handle="@keep:local", surface=getattr(ctx, "surface", None), owner=True)
    try:
        rows = G.list_grants(caller=keep, sources=_sources(params), now=now)
    except G.GrantRefused as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    first = G.earliest_expiry(rows)
    by_kind: dict[str, Any] = {}
    for kind in G.KINDS:
        e = G.earliest_expiry(g for g in rows if g.kind == kind)
        by_kind[kind] = e.expires_at.isoformat() if e and e.expires_at else None
    horizon = now + timedelta(hours=hours)
    due = [g.as_dict() for g in rows if g.expires_at is not None and g.expires_at <= horizon]
    return SkillResult(
        ok=True,
        value={
            "count": len(rows),
            "earliest": first.as_dict() if first else None,
            "by_kind": by_kind,
            "due": due,
            "within_hours": hours,
        },
        actions_taken=[f"read {len(rows)} grant(s); {len(due)} lapse within {hours:g}h"],
    )


_GRANT_NAMED = Precondition(
    "grant-named",
    "the grant to act on is named by its id",
    check=lambda params, ctx: bool((params or {}).get("id")),
    fix="pass id, as `grant list` shows it",
)
_GRANT_REF = Ref("grant_id", "pass as id to grant.show or grant.revoke", field="id")

SKILLS: tuple[SkillSpec, ...] = (
    SkillSpec(
        name="grant.list",
        fn=list_,
        description=(
            "Every grant in force that you hold, issued or (as an owner) govern: read, act, sign "
            "and run, in one shape (principal, scope, expiry, issuer, parent). Answers what a "
            "principal can do right now."
        ),
        inputs={"principal": "@name:context (optional)", "kind": "read|act|sign|run (optional)"},
        side_effects=False,
        idempotent=True,
        surfaces=_READ,
        risk="read",
        requires=(),
        produces=(_GRANT_REF,),
        next_steps=(NextStep("grant.show", "ok", "see one grant in full"),),
    ),
    SkillSpec(
        name="grant.show",
        fn=show,
        description="One grant in force, in the one shape, whichever store holds it.",
        inputs={"id": "the grant id, as grant.list shows it"},
        side_effects=False,
        idempotent=True,
        surfaces=_READ,
        risk="read",
        requires=(_GRANT_NAMED,),
        produces=(),
        next_steps=(),
    ),
    SkillSpec(
        name="grant.revoke",
        fn=revoke,
        description=(
            "End one grant in the store that holds it; its next use is refused there. Only its "
            "issuer, its principal or an owner may, at the command line."
        ),
        inputs={"id": "the grant id, as grant.list shows it", "reason": "why (recorded)"},
        side_effects=True,
        idempotent=True,
        surfaces=("cli",),
        risk="destructive",
        requires=(_GRANT_NAMED,),
        produces=(),
        next_steps=(NextStep("grant.list", "ok", "confirm it is gone"),),
    ),
    SkillSpec(
        name="vault.grant_expiry",
        fn=expiry,
        description=(
            "KEEP's grant-expiry pass: the earliest expiry across every kind of grant, and the "
            "grants lapsing within the horizon. Reads the one grant projection."
        ),
        inputs={"within_hours": "horizon in hours (default 24)"},
        side_effects=False,
        idempotent=True,
        surfaces=("cli",),
        risk="read",
        requires=(),
        produces=(),
        next_steps=(),
    ),
)


__all__ = ["EXPIRY_HORIZON", "SKILLS", "expiry", "list_", "revoke", "show"]
