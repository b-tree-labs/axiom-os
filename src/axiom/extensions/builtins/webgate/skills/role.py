# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``gate.role`` — change who may do what, on one account or every holder of a role.

Without it the only way to move people to a new role was to hand-edit the
accounts file on the node. Roles must be ones this node defines (built-in or
from the site's roles file): a key minted from an undefined role would
authorise nothing. Passwords, names and identity bindings are never touched.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult
from axiom.webauth import AccountsFileError, load_user_records, upsert_user_record

from ._accounts import ACCOUNTS_ENV, resolve_accounts_path


def _names(value: Any) -> list[str]:
    if value is None:
        return []
    items = [value] if isinstance(value, str) else list(value)
    return [str(v).strip() for v in items if str(v).strip()]


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    email = (params.get("email") or "").strip().lower()
    where = (params.get("where_role") or "").strip()
    set_to, add, remove = (_names(params.get(k)) for k in ("set", "add", "remove"))

    if bool(email) == bool(where):
        return SkillResult(ok=False, errors=["name one account (email) or every holder of a role (--where-role), not both"])
    if not (set_to or add or remove):
        return SkillResult(ok=False, errors=["nothing to change: pass --set, --add or --remove"])
    if set_to and (add or remove):
        return SkillResult(ok=False, errors=["--set replaces the roles; do not combine it with --add/--remove"])

    from ..role_bundles import default_bundle_registry

    known = set(default_bundle_registry().roles())
    undefined = [r for r in set_to + add if r not in known]
    if undefined:
        return SkillResult(ok=False, errors=[
            f"this node does not define the role(s) {', '.join(undefined)}; "
            f"defined: {', '.join(sorted(known))}"])

    path = resolve_accounts_path(params)
    if path is None:
        return SkillResult(ok=False, errors=[f"no accounts file — pass --accounts-file or set {ACCOUNTS_ENV}"])
    if not path.is_file():
        return SkillResult(ok=False, errors=[f"accounts file not found: {path}"])
    try:
        records = load_user_records(path)
    except AccountsFileError as e:
        return SkillResult(ok=False, errors=[str(e)])

    targets = [r for r in records if (r["email"] == email if email else where in r["roles"])]
    if email and not targets:
        return SkillResult(ok=False, errors=[f"no such account: {email}"])

    actions, changed = [], 0
    for rec in targets:
        before = list(rec["roles"])
        if set_to:
            after = list(dict.fromkeys(set_to))
        else:
            after = [r for r in before if r not in remove]
            after += [r for r in add if r not in after]
        if after == before:
            continue
        try:
            upsert_user_record(path, email=rec["email"], password_hash=None, roles=after, overwrite=True)
        except AccountsFileError as e:
            return SkillResult(ok=False, errors=[str(e)], actions_taken=actions)
        changed += 1
        actions.append(f"{rec['email']}: {', '.join(before) or '—'} → {', '.join(after) or '—'}")
    if not changed:
        actions.append("no account needed a change")
    return SkillResult(ok=True, value={"changed": changed, "accounts_file": str(path)}, actions_taken=actions)
