# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``program.invite`` / ``program.redeem`` — joining a program, the gate way.

A program invitation is a thin wrapper over the platform's own invitation
primitive, :mod:`axiom.webauth.invitations` (the self-service keys flow): the
deputy approves *who may join at which lane and role*, and a single-use,
expiring, scrypt-hashed-at-rest code is minted. The invited principal redeems
it — the code is the authentication — and redemption **records the program
membership** in ``people[]``. We reuse the primitive's lifecycle wholesale:
single-use, expiry, the shrink-only scope rule, the same at-rest hashing. We do
not re-implement any of it.

The program-specific facts (which program, lane, role, optional accounts) ride
as extra fields on the invitation record, which the primitive carries through
losslessly, and are read back at redemption to record the membership. The
primitive mints a webauth key on redemption; the program does not persist or
surface it — the program's concern is the membership, not a webauth credential
(a deployment that wants self-service keys too uses ``gate.redeem`` directly).

Only ``axiom.webauth`` (substrate, pure stdlib) is imported — no GitLab,
GitHub, harness, or network. An invited person who holds no tracker account is
onboarded normally (ADR-166: absence of an account is never a barrier).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..model import ProgramValidationError
from . import _changelog as cl
from ._mutate import (
    BAD_REQUEST,
    CONFLICT,
    FORBIDDEN,
    authorize,
    caller,
    commit,
    load_for_edit,
    log_only,
    mutable_copy,
    parse_accounts,
    refuse,
    valid_principal,
)

_UNITS = {"m": "minutes", "h": "hours", "d": "days"}
#: An unredeemed invitation lives a week by default — long enough to get to,
#: short enough that a forgotten one is not a standing grant.
_DEFAULT_EXPIRY = "7d"


def _parse_expiry(text: str) -> tuple[timedelta | None, str | None]:
    raw = (text or "").strip().lower()
    if len(raw) < 2 or raw[-1] not in _UNITS or not raw[:-1].isdigit():
        return None, f"cannot read {text!r} as a duration — use a number and one of m, h, d (e.g. 90m, 12h, 7d)"
    amount = int(raw[:-1])
    if amount <= 0:
        return None, "a duration has to be positive"
    return timedelta(**{_UNITS[raw[-1]]: amount}), None


def _invitations_path(params: dict[str, Any], ctx: SkillContext) -> Path:
    raw = params.get("invitations_file")
    if raw:
        return Path(raw)
    return ctx.state_dir / "program" / "invitations.json"


def _program_scopes(pid: str, lane: str, role: str | None) -> list[str]:
    """The invitation's scope ceiling — program-scoped, never a vendor scope."""
    scopes = [f"program:{pid}", f"lane:{lane}"]
    if role:
        scopes.append(f"role:{role}")
    return scopes


# ---- invite ---------------------------------------------------------------


def invite(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    data, path, bad = load_for_edit(params, ctx)
    if bad is not None:
        return bad
    assert data is not None and path is not None
    msg = authorize(data, ctx)
    if msg is not None:
        return refuse(FORBIDDEN, msg)

    principal = params.get("principal")
    if not valid_principal(principal):
        return refuse(BAD_REQUEST, f"{principal!r} is not a principal of the form @name or @name:context")
    lane = params.get("lane")
    if not isinstance(lane, str) or lane not in data.lane_ids():
        return refuse(BAD_REQUEST, f"--lane must name an existing lane (got {lane!r})")
    role = params.get("role") or None

    accounts, acc_err = parse_accounts(params.get("account"))
    if acc_err is not None:
        return refuse(BAD_REQUEST, acc_err)

    expires_in, exp_err = _parse_expiry(params.get("expires") or _DEFAULT_EXPIRY)
    if exp_err is not None:
        return refuse(BAD_REQUEST, exp_err)

    pid = data.program.get("id") or "program"

    from axiom.webauth.invitations import append_invitation, mint_invitation

    try:
        code, record = mint_invitation(
            principal=principal,
            roles=(),  # program roles are data, not gate role-bundles; scope is the ceiling
            scopes=_program_scopes(pid, lane, role),
            # The gate primitive binds its ``site`` to the principal's own
            # ``:context``; the program id rides as an embedded field instead,
            # so a member's context is never overridden by the program id.
            site=None,
            name=params.get("name") or "",
            invited_by=caller(ctx),
            expires_in=expires_in,
        )
    except ValueError as exc:
        return refuse(BAD_REQUEST, str(exc))

    # Program-specific facts ride on the record; the primitive carries them
    # through losslessly and hands them back at redemption.
    record["program"] = pid
    record["lane"] = lane
    record["role"] = role
    record["accounts"] = accounts or {}

    invitations_path = _invitations_path(params, ctx)
    append_invitation(invitations_path, record)

    recorded = log_only(
        ctx,
        [
            {
                "kind": "invited",
                "subject_kind": cl.SUBJECT_INVITATION,
                "subject": record["invitation_id"],
                "field": None,
                "old": None,
                "new": {"principal": principal, "lane": lane, "role": role},
            }
        ],
    )

    return SkillResult(
        ok=True,
        value={
            "invitation_id": record["invitation_id"],
            "code": code,  # shown once — it is a redemption token, not a credential
            "principal": principal,
            "lane": lane,
            "role": role,
            "expires_at": record["expires_at"],
            "path": str(invitations_path),
            "changes": recorded,
        },
        actions_taken=[f"issued invitation {record['invitation_id']} for {principal} to join {lane}"],
    )


# ---- redeem ---------------------------------------------------------------


def redeem(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Redeem an invitation and record the membership.

    Not deputy-gated: the code is the authentication (the invitation was
    deputy-authorized when it was issued). Honors the missing-account
    onboarding finding — an invited person who declares no accounts becomes a
    member with none, not a blocked one.
    """
    data, path, bad = load_for_edit(params, ctx)
    if bad is not None:
        return bad
    assert data is not None and path is not None

    code = (params.get("code") or "").strip()
    if not code:
        return refuse(BAD_REQUEST, "pass the invitation code you were given (--code)")

    invitations_path = _invitations_path(params, ctx)

    from axiom.webauth.invitations import (
        InvitationRefused,
        InvitationsFileError,
        redeem_invitation,
    )

    try:
        # roles=() on the record, so resolve_roles is never consulted; the
        # ceiling scopes are granted as-is (shrink-only can never widen them).
        _token, _key_record, invitation = redeem_invitation(
            invitations_path, code, resolve_roles=lambda names: []
        )
    except InvitationRefused as exc:
        return refuse(FORBIDDEN, str(exc))
    except InvitationsFileError as exc:
        return refuse(BAD_REQUEST, str(exc))

    principal = invitation.get("principal")
    lane = invitation.get("lane")
    role = invitation.get("role")
    accounts = invitation.get("accounts") or {}

    if not valid_principal(principal):
        return refuse(BAD_REQUEST, f"the invitation names a malformed principal {principal!r}")
    if lane is not None and lane not in data.lane_ids():
        return refuse(CONFLICT, f"the invitation is for lane {lane!r}, which no longer exists; ask for a new one")

    new_data = mutable_copy(data)
    existing = new_data.person(principal)
    extra: list[dict[str, Any]] = []
    if existing is None:
        member: dict[str, Any] = {"principal": principal, "lanes": [lane] if lane else []}
        if role:
            member["role"] = role
        if accounts:
            member["accounts"] = accounts
        new_data.raw.setdefault("people", []).append(member)
        extra.append(
            {
                "kind": "person_added",
                "subject_kind": cl.SUBJECT_PERSON,
                "subject": principal,
                "field": None,
                "old": None,
                "new": {"lanes": member["lanes"], "role": role, "via": "redeem"},
            }
        )
    else:
        member = existing
        old = {"lanes": list(member.get("lanes", [])), "role": member.get("role")}
        if lane and lane not in member.get("lanes", []):
            member.setdefault("lanes", []).append(lane)
        if role:
            member["role"] = role
        if accounts:
            merged = dict(member.get("accounts") or {})
            merged.update(accounts)
            member["accounts"] = merged
        extra.append(
            {
                "kind": "person_reassigned",
                "subject_kind": cl.SUBJECT_PERSON,
                "subject": principal,
                "field": None,
                "old": old,
                "new": {"lanes": member.get("lanes", []), "role": member.get("role"), "via": "redeem"},
            }
        )

    extra.append(
        {
            "kind": "redeemed",
            "subject_kind": cl.SUBJECT_INVITATION,
            "subject": invitation.get("invitation_id"),
            "field": None,
            "old": None,
            "new": {"principal": principal, "lane": lane, "role": role},
        }
    )

    try:
        recorded = commit(ctx, path, data, new_data, extra)
    except ProgramValidationError as exc:
        return refuse(BAD_REQUEST, "; ".join(exc.errors))

    return SkillResult(
        ok=True,
        value={
            "principal": principal,
            "lane": lane,
            "role": role,
            "member": member,
            "invitation_id": invitation.get("invitation_id"),
            "changes": recorded,
        },
        actions_taken=[f"redeemed invitation {invitation.get('invitation_id')}; recorded {principal} as a member"],
    )
