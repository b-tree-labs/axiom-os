# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Device enrolment and location codes (ADR-146). Administration, CLI only.

Enrolling a device or creating a location's secret grants no authority to
sign anything (ADR-142 rule 8); it only states what a device is and where.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from axiom.infra.skills import SkillResult

from .. import devices, presence, store
from ..db_models import AttestDevice
from ._common import fail, resolve_site


def device_enroll(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    site, err = resolve_site(params)
    if err:
        return fail(err)
    try:
        e = devices.enroll(
            site_id=site,
            device_id=params.get("device_id") or "",
            device_class=params.get("device_class") or "",
            location=params.get("location"),
            mobility=params.get("mobility") or "fixed",
            by=ctx.principal.handle if ctx else "unknown",
        )
        claim = devices.issue_claim_code(e.device_id)
    except ValueError as exc:
        return fail(str(exc))
    return SkillResult(
        ok=True,
        value={"device": e.__dict__, "claim": _claim(e.device_id, claim)},
        actions_taken=[
            f"enrolled {e.device_id} as {e.device_class} at {e.location or 'no location'}"
        ],
    )


def _claim(device_id: str, code: str) -> dict[str, Any]:
    minutes = int(devices.CLAIM_TTL.total_seconds() // 60)
    return {
        "code": code,
        "expires_in_minutes": minutes,
        "how": f"on {device_id}'s browser, open /gate/devices/claim and enter this code",
    }


def device_reclaim(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Issue a fresh one-time claim code, e.g. after a browser's data was cleared."""
    try:
        code = devices.issue_claim_code(params.get("device_id") or "")
    except ValueError as exc:
        return fail(str(exc))
    return SkillResult(ok=True, value={"claim": _claim(params["device_id"], code)})


def device_retire(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    try:
        devices.retire(params.get("device_id") or "", by=ctx.principal.handle if ctx else "unknown")
    except ValueError as exc:
        return fail(str(exc))
    return SkillResult(ok=True, value={"retired": params["device_id"]})


def device_list(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    site, err = resolve_site(params)
    if err:
        return fail(err)
    with store.session_scope() as s:
        rows = s.execute(
            select(AttestDevice)
            .where(AttestDevice.site_id == site, AttestDevice.retired_at.is_(None))
            .order_by(AttestDevice.device_id)
        ).scalars()
        out = [
            {
                "device_id": r.device_id,
                "device_class": r.device_class,
                "location": r.location,
                "mobility": r.mobility,
            }
            for r in rows
        ]
    return SkillResult(ok=True, value={"site_id": site, "devices": out})


def location_init(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    site, err = resolve_site(params)
    if err:
        return fail(err)
    try:
        presence.init_location(site, params.get("location") or "")
    except ValueError as exc:
        return fail(str(exc))
    return SkillResult(
        ok=True,
        value={"site_id": site, "location": params["location"], "secret": "stored in the vault"},
    )


def location_code(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """What the location's fixed display shows now. Run it on that display."""
    site, err = resolve_site(params)
    if err:
        return fail(err)
    code = presence.current_code(site, params.get("location") or "")
    if code is None:
        return fail(f"location {params.get('location')!r} has no secret; run location init")
    return SkillResult(
        ok=True,
        value={
            "location": params["location"],
            "code": code,
            "window_seconds": presence.WINDOW_SECONDS,
        },
    )


def role_grant(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Give a person a signing role at a site (administration; logged)."""
    site, err = resolve_site(params)
    if err:
        return fail(err)
    from .. import roles

    by = ctx.principal.handle if ctx else "unknown"
    try:
        roles.grant(
            params.get("principal") or "",
            site,
            params.get("role") or "",
            by=by,
            state_dir=ctx.state_dir,
            logbook=params.get("logbook") or None,
            until=params.get("until") or None,
        )
    except ValueError as exc:
        return fail(str(exc))
    scope = f" for {params['logbook']} until {params['until']}" if params.get("logbook") else ""
    return SkillResult(
        ok=True,
        value={"granted": params["role"], "principal": params["principal"], "site_id": site,
               "logbook": params.get("logbook"), "until": params.get("until"), "by": by},  # fmt: skip
        actions_taken=[f"granted {params['role']} to {params['principal']} at {site}{scope}, by {by}"],
    )


def role_revoke(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    site, err = resolve_site(params)
    if err:
        return fail(err)
    from .. import roles

    try:
        roles.revoke(
            params.get("principal") or "",
            site,
            params.get("role") or "",
            by=ctx.principal.handle if ctx else "unknown",
            state_dir=ctx.state_dir,
            logbook=params.get("logbook") or None,
        )
    except ValueError as exc:
        return fail(str(exc))
    return SkillResult(
        ok=True,
        value={"revoked": params["role"], "principal": params["principal"], "site_id": site},
        actions_taken=[f"revoked {params['role']} from {params['principal']} at {site}"],
    )


def role_list(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    site, err = resolve_site(params)
    if err:
        return fail(err)
    from .. import roles

    assignments = roles.assignments(site, state_dir=ctx.state_dir)
    if params.get("aliases"):
        # Each role with every spelling that resolves to it (O1: hyphen and underscore are one).
        assignments = [
            {**a, "spellings": {r: sorted({r, r.replace("_", "-")}) for r in a["roles"]}}
            for a in assignments
        ]
    return SkillResult(
        ok=True,
        value={"site_id": site, "assignments": assignments,
               "log_roles": roles.log_assignments(site, state_dir=ctx.state_dir)},  # fmt: skip
    )


def my_roles(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """The signing roles the caller holds at a site, in their one canonical spelling.

    Read only, and only the caller's own: it never lists anyone else and states nothing a person
    could grant themselves (ADR-142: a person never states their own roles).
    """
    site, err = resolve_site(params)
    if err:
        return fail(err)
    from .. import roles

    handle = ctx.principal.handle if ctx and ctx.principal else ""
    if not handle:
        return fail("no signed-in person to look up")
    held = roles.roles_for(handle, site, state_dir=ctx.state_dir)
    return SkillResult(ok=True, value={"site_id": site, "principal": handle, "roles": list(held)})


# -- gate pairs (ADR-284) ------------------------------------------------------------------


def _propose_pair_change(params: dict[str, Any], ctx: Any, *, remove: bool) -> SkillResult:
    """Draft the signature on a change to the site's gate pairs. Nothing is changed here: the
    command line cannot prove a fresh sign-in, so the person signs the draft where that can be
    proved, and signing applies it."""
    site, err = resolve_site(params)
    if err:
        return fail(err)
    from .. import roles, settings_authority

    handle = str(getattr(getattr(ctx, "principal", None), "handle", "") or "")
    if not handle.startswith("@"):
        return fail("a change to a gate pair is proposed by a person; nothing was proposed")
    if settings_authority.ROLE not in roles.roles_for(handle, site, state_dir=ctx.state_dir):
        return fail(
            f"proposing a change to a gate pair needs the {settings_authority.ROLE} role at {site}. "
            f"An administrator can give it: axi log role grant {handle} {settings_authority.ROLE}"
        )
    gate = str(params.get("gate_role") or "").strip()
    ledger = settings_authority.node_ledger(site)
    current = dict(ledger.value(settings_authority.SECTION, settings_authority.GATE_ROLES) or {})
    if remove:
        if gate not in current:
            return fail(f"there is no pair for the gate role {gate!r} to remove")
        after = {g: s for g, s in current.items() if g != gate}
    else:
        signing_role = str(params.get("signing_role") or "").strip()
        problem = roles.pair_problem(gate, signing_role)
        if problem:
            return fail(problem)
        after = {**current, gate: roles.canonical_signing_role(signing_role)}
        if after == current:
            return fail(f"{gate} is already paired with {after[gate]}")
    revision = ledger.revision(settings_authority.SECTION, settings_authority.GATE_ROLES) + 1
    draft_id = settings_authority.create_change_draft(
        site_id=site,
        section=settings_authority.SECTION,
        key=settings_authority.GATE_ROLES,
        op="set" if after else "unset",
        value=after or None,
        revision=revision,
        for_principal=handle,
    )
    what = f"remove the pair for {gate}" if remove else f"pair {gate} with {after[gate]}"
    return SkillResult(
        ok=True,
        value={"draft_id": draft_id, "site_id": site, "pairs_after": after, "revision": revision},
        actions_taken=[
            f"proposed: {what}. Nothing has changed yet. Sign it in the web app (Waiting for you), "
            f"where a fresh sign-in can be proved; signing applies it. Draft {draft_id}."
        ],
    )


def gate_pair_set(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Propose that a person who signs in with a gate role holds a signing role (administration)."""
    return _propose_pair_change(params, ctx, remove=False)


def gate_pair_unset(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Propose removing the pair for a gate role."""
    return _propose_pair_change(params, ctx, remove=True)


def gate_pair_list(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """The gate pairs in force, and any entry in the ledger that is ignored and why."""
    site, err = resolve_site(params)
    if err:
        return fail(err)
    from .. import settings_authority

    ledger = settings_authority.node_ledger(site)
    return SkillResult(
        ok=True,
        value={
            "site_id": site,
            "pairs": dict(ledger.value(settings_authority.SECTION, settings_authority.GATE_ROLES) or {}),
            "ignored": [
                {"seq": u.seq, "key": u.key, "reason": u.reason} for u in ledger.unverified()
            ],
            "damage": ledger.damage(),
        },
    )
