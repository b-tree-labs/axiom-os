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

    try:
        roles.grant(
            params.get("principal") or "",
            site,
            params.get("role") or "",
            by=ctx.principal.handle if ctx else "unknown",
            state_dir=ctx.state_dir,
        )
    except ValueError as exc:
        return fail(str(exc))
    return SkillResult(
        ok=True,
        value={"granted": params["role"], "principal": params["principal"], "site_id": site},
        actions_taken=[f"granted {params['role']} to {params['principal']} at {site}"],
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

    return SkillResult(
        ok=True,
        value={"site_id": site, "assignments": roles.assignments(site, state_dir=ctx.state_dir)},
    )
