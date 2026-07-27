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
    load_user_records,
)

from ._accounts import (
    ACCOUNTS_ENV,
    KEYS_ENV,
    resolve_accounts_path,
    resolve_keys_path,
)

_RESOURCES = ("accounts", "api-keys")


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    resource = (params.get("resource") or "accounts").strip()
    if resource not in _RESOURCES:
        return SkillResult(ok=False, errors=[
            f"unknown resource {resource!r} — expected one of {list(_RESOURCES)}"])
    if resource == "api-keys":
        return _list_api_keys(params)
    return _list_accounts(params)


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

    items = [
        {
            "key_id": r["key_id"],
            "principal": r["principal"],
            "scopes": list(r["scopes"]),
            "name": r["name"],
            "created_at": r["created_at"],
            "revoked_at": r["revoked_at"],
        }
        for r in records
    ]
    return SkillResult(
        ok=True,
        value={"items": items, "keys_file": str(path)},
        actions_taken=[f"{len(items)} API key(s) in {path}"],
    )
