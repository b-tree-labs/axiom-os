# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``gate.invite`` — approve who may have a key, without minting it yourself.

``gate.issue`` is the right verb when the holder is a service. For a person it
puts their secret in the administrator's terminal, and onboarding three
colleagues on 2026-10-01 did exactly that three times; one of the keys reached a
chat paste and had to be rotated.

An administrator decides **who may have what**. This verb records that decision
and prints a code that does one thing: redeem itself, once, before it expires.
The key is minted on the colleague's side by ``gate.redeem``.

Scopes come from the role bundles at this moment and are written down as the
ceiling. See :mod:`axiom.webauth.invitations` for why redemption may shrink that
and never widen it.
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult
from axiom.webauth.invitations import append_invitation, mint_invitation

from ._accounts import resolve_keys_path

INVITES_ENV = "AXIOM_GATE_INVITATIONS_FILE"

#: How long an unredeemed invitation lives when nobody says. Short on purpose: a
#: week is long enough for somebody to get to it and short enough that a
#: forgotten one is not a standing grant.
DEFAULT_EXPIRY = timedelta(days=7)

_UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def resolve_invitations_path(params: dict[str, Any]) -> Path | None:
    """The invitations file, from ``--invitations-file``, the environment, or
    beside the keys file — which is where an operator will look for it."""
    raw = params.get("invitations_file") or os.environ.get(INVITES_ENV)
    if raw:
        return Path(raw)
    keys = resolve_keys_path(params)
    return keys.with_name("invitations.json") if keys else None


def parse_expiry(text: str) -> timedelta:
    """``7d`` / ``12h`` / ``90m``. Raises ValueError naming the accepted forms."""
    raw = (text or "").strip().lower()
    if len(raw) < 2 or raw[-1] not in _UNITS or not raw[:-1].isdigit():
        raise ValueError(
            f"cannot read {text!r} as a duration — use a number and one of "
            f"m, h, d (for example 90m, 12h, 7d)"
        )
    amount = int(raw[:-1])
    if amount <= 0:
        raise ValueError("a duration has to be positive")
    return timedelta(**{_UNITS[raw[-1]]: amount})


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    principal = (params.get("principal") or "").strip()
    if not principal:
        return SkillResult(ok=False, errors=[
            "--principal is required: the person this invitation is for "
            "(@name:context, e.g. @alex:site)"])

    roles = tuple(r for r in (params.get("role") or ()) if r)
    explicit = tuple(s for s in (params.get("scope") or ()) if s)
    if not roles and not explicit:
        return SkillResult(ok=False, errors=[
            "an invitation has to grant something: pass --role (resolved through "
            "the registered role bundles) or --scope"])

    from ..role_bundles import default_bundle_registry

    registry = default_bundle_registry()
    scopes = set(explicit)
    if roles:
        unknown = [r for r in roles if r not in registry.roles()]
        if unknown:
            return SkillResult(ok=False, errors=[
                f"no role bundle named {', '.join(unknown)} "
                f"(registered: {', '.join(registry.roles()) or '(none)'})"])
        scopes |= set(registry.resolve(roles))
    if not scopes:
        return SkillResult(ok=False, errors=[
            f"the role(s) {', '.join(roles)} resolve to no scopes, so this "
            "invitation would grant nothing"])

    try:
        expires_in = parse_expiry(params.get("expires") or "7d")
    except ValueError as exc:
        return SkillResult(ok=False, errors=[str(exc)])

    path = resolve_invitations_path(params)
    if path is None:
        return SkillResult(ok=False, errors=[
            f"no invitations file: pass --invitations-file or set ${INVITES_ENV} "
            "(it defaults to sitting beside the API-keys file)"])

    try:
        code, record = mint_invitation(
            principal=principal,
            roles=roles,
            scopes=tuple(sorted(scopes)),
            site=params.get("site") or os.environ.get("AXIOM_SITE") or None,
            name=params.get("name") or "",
            invited_by=params.get("invited_by") or "",
            expires_in=expires_in,
        )
        append_invitation(path, record)
    except ValueError as exc:
        return SkillResult(ok=False, errors=[str(exc)])

    return SkillResult(
        ok=True,
        value={
            "invitation_id": record["invitation_id"],
            "code": code,
            "principal": record["principal"],
            "roles": record["roles"],
            "scopes": record["scopes"],
            "site": record["site"],
            "expires_at": record["expires_at"],
            "path": str(path),
        },
        actions_taken=[f"recorded invitation {record['invitation_id']} for {record['principal']}"],
    )
