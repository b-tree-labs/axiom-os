# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``gate.adduser`` — add a password account to the gate's accounts file.

Replaces the hand-edit-JSON-and-hash provisioning dance with one verb: hash the
password (scrypt), validate the record, and append it atomically. The running
gate picks it up on the next login via the file store's mtime reload — no
restart. Roles are free-form strings the gate carries in the session; the
consumer layer decides what they mean.
"""

from __future__ import annotations

import os

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult
from axiom.webauth import (
    AccountsFileError,
    get_password_hash,
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

    supplied = params.get("password")
    generated = not supplied
    password = supplied or generate_password()
    if supplied:
        ok, msg = validate_password(password, complexity="standard")
        if not ok:
            return SkillResult(ok=False, errors=[f"weak password: {msg}"])

    roles = tuple(params.get("role") or ())
    site = params.get("site") or os.environ.get("AXIOM_SITE") or None

    # --apply-bundle: resolve the given roles through the registered role
    # bundles and pin the union onto the account record. Opt-in only —
    # without the flag, roles stay the free-form strings they always were.
    attributes = None
    bundle_scopes: list[str] | None = None
    if params.get("apply_bundle"):
        from ..role_bundles import default_bundle_registry

        bundle_scopes = list(default_bundle_registry().resolve(roles))
        if not bundle_scopes:
            return SkillResult(ok=False, errors=[
                "--apply-bundle: none of the given --role values matches a "
                f"registered role bundle (registered: "
                f"{', '.join(default_bundle_registry().roles()) or '(none)'})"])
        attributes = {"bundle_scopes": bundle_scopes}

    try:
        out = upsert_user_record(
            path,
            email=email,
            password_hash=get_password_hash(password),
            name=params.get("name"),
            roles=roles,
            user_id=params.get("user_id"),
            attributes=attributes,
            overwrite=bool(params.get("force")),
            site=site,
        )
    except (AccountsFileError, ValueError) as e:
        return SkillResult(ok=False, errors=[str(e)])

    verb = "updated" if not out["created"] else "added"
    role_str = ", ".join(out["roles"]) or "(none)"
    actions = [f"{verb} account {out['email']} — roles: {role_str} → {path}"]
    value = {
        "email": out["email"],
        "user_id": out["user_id"],
        "roles": out["roles"],
        "site": out.get("site"),
        "created": out["created"],
        "accounts_file": str(path),
    }
    if bundle_scopes is not None:
        value["bundle_scopes"] = bundle_scopes
        actions.append(
            f"applied role bundle(s) — scopes: {', '.join(bundle_scopes)}")
    if generated:
        value["password"] = password
        actions.append(
            f"temporary password: {password}  "
            "(shown once — relay it securely; the user signs in with it)")
    if not roles:
        actions.append(
            "no roles set — this account gets the consumer's least-privilege "
            "default until you assign one")
    return SkillResult(ok=True, value=value, actions_taken=actions)
