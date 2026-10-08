# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``gate.redeem`` — spend an invitation and get your own key.

The other half of ``gate.invite``. The person who will hold the key runs this,
so the plaintext exists once, on their machine, and never in the administrator's
terminal or in whatever channel carried the invitation.

Roles are resolved through the registered bundles **now**, not at invitation
time, and the redemption is refused if that would grant more than the invitation
recorded. :mod:`axiom.webauth.invitations` carries the reasoning; the short
version is that an invitation can only shrink, which is what makes an
outstanding one safe.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult
from axiom.webauth import ApiKeysFileError, append_api_key_record
from axiom.webauth.invitations import (
    InvitationRefused,
    InvitationsFileError,
    redeem_invitation,
)

from ._accounts import KEYS_ENV, resolve_keys_path
from .invite import INVITES_ENV, resolve_invitations_path


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    code = (params.get("code") or "").strip()
    if not code:
        return SkillResult(ok=False, errors=[
            "pass the invitation code you were sent"])

    invitations_path = resolve_invitations_path(params)
    if invitations_path is None:
        return SkillResult(ok=False, errors=[
            f"no invitations file: pass --invitations-file or set ${INVITES_ENV}"])
    keys_path = resolve_keys_path(params)
    if keys_path is None:
        return SkillResult(ok=False, errors=[
            f"no API-keys file: pass --keys-file or set ${KEYS_ENV}"])

    from ..role_bundles import default_bundle_registry

    registry = default_bundle_registry()

    def _resolve(names):
        """Resolve role names, raising KeyError for one that is gone.

        The registry returns a union and says nothing about a name it did not
        recognise, which would let a vanished role read as a narrowing instead of
        an error. So each name is checked before the union is taken.
        """
        known = set(registry.roles())
        for name in names:
            if name not in known:
                raise KeyError(name)
        return registry.resolve(tuple(names))

    try:
        token, key_record, invitation = redeem_invitation(
            invitations_path, code, resolve_roles=_resolve
        )
    except InvitationRefused as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    except InvitationsFileError as exc:
        return SkillResult(ok=False, errors=[str(exc)])

    try:
        append_api_key_record(keys_path, key_record)
    except ApiKeysFileError as exc:
        return SkillResult(ok=False, errors=[
            f"the invitation was spent but the key could not be stored: {exc}. "
            f"Ask for a new invitation — this one cannot be redeemed again"])

    return SkillResult(
        ok=True,
        value={
            # Shown once. The caller is the holder, which is the point.
            "token": token,
            "key_id": key_record["key_id"],
            "principal": key_record["principal"],
            "scopes": key_record["scopes"],
            "site": key_record["site"],
            "invited_by": invitation.get("invited_by") or "",
        },
        actions_taken=[
            f"redeemed invitation {invitation['invitation_id']}",
            f"stored key {key_record['key_id']} in {keys_path}",
        ],
    )
