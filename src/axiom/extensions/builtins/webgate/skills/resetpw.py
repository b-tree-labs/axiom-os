# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``gate.resetpw`` — rotate an existing account's password.

The admin-mediated half of "forgot password": when a user is locked out, an
admin sets (or auto-generates) a new password in one command. Roles and name
are preserved. The running gate picks up the new hash on the next login via the
file store's mtime reload — no restart.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult
from axiom.webauth import (
    AccountsFileError,
    get_password_hash,
    load_user_records,
    upsert_user_record,
    validate_password,
)

from ._accounts import ACCOUNTS_ENV, generate_password, resolve_accounts_path


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    email = (params.get("email") or "").strip()
    if not email:
        return SkillResult(ok=False, errors=["missing required argument: email"])

    path = resolve_accounts_path(params)
    if path is None:
        return SkillResult(ok=False, errors=[
            f"no accounts file — pass --accounts-file or set {ACCOUNTS_ENV}"])
    if not path.is_file():
        return SkillResult(ok=False, errors=[f"accounts file not found: {path}"])

    # Only reset an account that exists — never create one by side effect.
    try:
        records = load_user_records(path)
    except AccountsFileError as e:
        return SkillResult(ok=False, errors=[str(e)])
    norm = email.strip().lower()
    if not any(r["email"] == norm for r in records):
        return SkillResult(ok=False, errors=[f"no such account: {norm}"])

    supplied = params.get("password")
    generated = not supplied
    password = supplied or generate_password()
    if supplied:
        ok, msg = validate_password(password, complexity="standard")
        if not ok:
            return SkillResult(ok=False, errors=[f"weak password: {msg}"])

    try:
        out = upsert_user_record(
            path, email=norm, password_hash=get_password_hash(password),
            overwrite=True,
        )
    except AccountsFileError as e:
        return SkillResult(ok=False, errors=[str(e)])

    actions = [f"reset password for {out['email']} → {path}"]
    value = {"email": out["email"], "accounts_file": str(path)}
    if generated:
        value["password"] = password
        actions.append(
            f"temporary password: {password}  "
            "(shown once — relay it securely; the user signs in with it)")
    return SkillResult(ok=True, value=value, actions_taken=actions)
