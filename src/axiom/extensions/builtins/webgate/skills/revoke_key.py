# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``gate.revoke`` — revoke an issued API key or an unspent invitation, effective immediately.

Sets ``revoked_at`` on the record; the atomic rewrite bumps the file mtime
the authz hook's store watches, so the very next request presenting the key
is denied — no restart, no cache to expire. Idempotent: revoking an
already-revoked key reports the original revocation time.

``invitation`` withdraws an invitation nobody has redeemed yet: the next
presentation of its code is refused as revoked. See
:func:`axiom.webauth.invitations.revoke_invitation`.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult
from axiom.webauth import ApiKeysFileError, revoke_api_key_record
from axiom.webauth.invitations import (
    InvitationRefused,
    InvitationsFileError,
    revoke_invitation,
)

from ._accounts import KEYS_ENV, resolve_keys_path
from .invite import INVITES_ENV, resolve_invitations_path

_RESOURCES = ("api-key", "invitation")


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    resource = (params.get("resource") or "api-key").strip()
    if resource not in _RESOURCES:
        return SkillResult(
            ok=False, errors=[f"unknown resource {resource!r} — expected one of {list(_RESOURCES)}"]
        )

    if resource == "invitation":
        return _revoke_invitation(params)

    key_id = (params.get("key_id") or "").strip()
    if not key_id:
        return SkillResult(ok=False, errors=["missing required argument: key_id"])

    path = resolve_keys_path(params)
    if path is None:
        return SkillResult(ok=False, errors=[f"no keys file — pass --keys-file or set {KEYS_ENV}"])

    try:
        record = revoke_api_key_record(path, key_id)
    except ApiKeysFileError as e:
        return SkillResult(ok=False, errors=[str(e)])

    return SkillResult(
        ok=True,
        value={
            "key_id": record["key_id"],
            "principal": record["principal"],
            "revoked_at": record["revoked_at"],
            "keys_file": str(path),
        },
        actions_taken=[
            f"revoked API key {record['key_id']} ({record['principal']}) "
            f"at {record['revoked_at']} — effective on the next request",
        ],
    )


def _revoke_invitation(params: dict[str, Any]) -> SkillResult:
    invitation_id = (params.get("invitation_id") or params.get("key_id") or "").strip()
    if not invitation_id:
        return SkillResult(
            ok=False,
            errors=[
                "missing required argument: invitation_id (`gate list invitations` shows them)"
            ],
        )

    path = resolve_invitations_path(params)
    if path is None:
        return SkillResult(
            ok=False, errors=[f"no invitations file: pass --invitations-file or set ${INVITES_ENV}"]
        )

    try:
        record = revoke_invitation(path, invitation_id)
    except (InvitationRefused, InvitationsFileError) as e:
        return SkillResult(ok=False, errors=[str(e)])

    return SkillResult(
        ok=True,
        value={
            "invitation_id": record["invitation_id"],
            "principal": record["principal"],
            "revoked_at": record["revoked_at"],
            "invitations_file": str(path),
        },
        actions_taken=[
            f"revoked invitation {record['invitation_id']} ({record['principal']}) "
            f"at {record['revoked_at']} — its code is refused from now on",
        ],
    )
