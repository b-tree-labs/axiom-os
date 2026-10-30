# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``gate.list`` — list gate accounts or issued API keys (never hashes).

Read-only. The resource positional picks what to list: ``accounts`` (the
default — email, name, roles) or ``api-keys`` (key id, principal, scopes,
created/revoked). A not-yet-created file lists as empty (deny-all) rather
than erroring, so an admin can check state before provisioning.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult
from axiom.webauth import (
    AccountsFileError,
    ApiKeysFileError,
    load_api_key_records,
    load_last_used,
    load_user_records,
)

from ._accounts import (
    ACCOUNTS_ENV,
    KEYS_ENV,
    resolve_accounts_path,
    resolve_keys_path,
)

_RESOURCES = ("accounts", "api-keys", "invitations")


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    resource = (params.get("resource") or "accounts").strip()
    if resource not in _RESOURCES:
        return SkillResult(ok=False, errors=[
            f"unknown resource {resource!r} — expected one of {list(_RESOURCES)}"])
    if resource == "api-keys":
        return _list_api_keys(params)
    if resource == "invitations":
        return _list_invitations(params)
    return _list_accounts(params)


def _list_invitations(params: dict[str, Any]) -> SkillResult:
    from axiom.webauth.invitations import InvitationsFileError, list_invitations

    from .invite import INVITES_ENV, resolve_invitations_path

    path = resolve_invitations_path(params)
    if path is None:
        return SkillResult(ok=False, errors=[
            f"no invitations file: pass --invitations-file or set ${INVITES_ENV}"])
    try:
        # `list_invitations` strips the stored hash; there is no code to show.
        records = list_invitations(path)
    except InvitationsFileError as e:
        return SkillResult(ok=False, errors=[str(e)])

    items = [
        {
            "invitation_id": r["invitation_id"],
            "principal": r.get("principal"),
            "site": r.get("site"),
            "roles": list(r.get("roles") or ()),
            "scopes": list(r.get("scopes") or ()),
            "name": r.get("name") or "",
            "invited_by": r.get("invited_by") or "",
            "created_at": r.get("created_at"),
            "expires_at": r.get("expires_at"),
            "redeemed_at": r.get("redeemed_at"),
            "revoked_at": r.get("revoked_at"),
            "key_id": r.get("key_id"),
            "status": r["status"],
        }
        for r in records
    ]
    return SkillResult(
        ok=True,
        value={"items": items, "invitations_file": str(path)},
        actions_taken=[f"{len(items)} invitation(s) in {path}"],
    )


def present(result: SkillResult) -> list[str] | None:
    """Text lines for ``list invitations``; ``None`` for the other resources."""
    value = result.value
    if not (isinstance(value, dict) and "invitations_file" in value):
        return None
    from axiom.infra.cli_format import Column, table

    items = value.get("items") or []
    if not items:
        return ["  (no invitations)"]
    rows = [
        [
            i["invitation_id"], str(i.get("principal") or ""), i["status"],
            ", ".join(i.get("roles") or []) or "—", str(i.get("expires_at") or ""),
        ]
        for i in items
    ]
    cols = [Column(header="ID"), Column(header="PRINCIPAL", wrap=True), Column(header="STATUS"),
            Column(header="ROLES", wrap=True), Column(header="EXPIRES")]
    return table(rows, cols, headers=True)


def _list_accounts(params: dict[str, Any]) -> SkillResult:
    path = resolve_accounts_path(params)
    if path is None:
        return SkillResult(ok=False, errors=[
            f"no accounts file — pass --accounts-file or set {ACCOUNTS_ENV}"])

    if not path.is_file():
        return SkillResult(
            ok=True,
            value={"items": [], "accounts_file": str(path)},
            actions_taken=[f"accounts file not created yet: {path}"],
        )

    try:
        records = load_user_records(path)
    except AccountsFileError as e:
        return SkillResult(ok=False, errors=[str(e)])

    items = [
        {
            "email": r["email"],
            "name": r["name"],
            "roles": list(r["roles"]),
            "user_id": r["user_id"],
            "site": r.get("site"),
        }
        for r in records
    ]
    return SkillResult(
        ok=True,
        value={"items": items, "accounts_file": str(path)},
        actions_taken=[f"{len(items)} account(s) in {path}"],
    )


def _list_api_keys(params: dict[str, Any]) -> SkillResult:
    path = resolve_keys_path(params)
    if path is None:
        return SkillResult(ok=False, errors=[
            f"no keys file — pass --keys-file or set {KEYS_ENV}"])

    if not path.is_file():
        return SkillResult(
            ok=True,
            value={"items": [], "keys_file": str(path)},
            actions_taken=[f"keys file not created yet: {path}"],
        )

    try:
        records = load_api_key_records(path)
    except ApiKeysFileError as e:
        return SkillResult(ok=False, errors=[str(e)])

    used = load_last_used(path)
    items = [
        {
            "key_id": r["key_id"],
            "principal": r["principal"],
            "site": r.get("site"),
            "scopes": list(r["scopes"]),
            "name": r["name"],
            "created_at": r["created_at"],
            "revoked_at": r["revoked_at"],
            # When it last authenticated, to the minute; None until a use is recorded.
            "last_used_at": used.get(r["key_id"]),
            # Authenticates, but reads no site's data until it is tied to a site.
            "unsited": not r.get("site"),
        }
        for r in records
    ]
    unsited = [i for i in items if i["unsited"] and not i["revoked_at"]]
    notes = [f"{len(items)} API key(s) in {path}"]
    notes += [f"key {i['key_id']} ({i['principal']}) carries no site, so it reads no site's "
              f"data; tie it in place, same token: axi gate rescope api-key {i['key_id']} "
              f"--site <site>"
              for i in unsited]
    return SkillResult(
        ok=True,
        value={"items": items, "keys_file": str(path), "unsited": [i["key_id"] for i in unsited]},
        actions_taken=notes,
    )
