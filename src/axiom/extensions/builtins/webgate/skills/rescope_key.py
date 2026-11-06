# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``gate.rescope`` — change an issued key's site or scopes in place, keeping its token.

For moving a holder to a narrower rule without asking them to do anything: the
record's site and scopes change, its id, principal and hash do not, so the token
they already hold authenticates exactly as before and is held to the new site or
scopes on its next request (the authz store reloads on the atomic rewrite).

Mutating, so the universal action audit records who rescoped which key and how.
A key already tied to a site cannot be moved to another; issue one for that site.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult
from axiom.webauth import ApiKeysFileError, rescope_api_key_record

from ._accounts import KEYS_ENV, resolve_keys_path

_RESOURCES = ("api-key",)


def _list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    resource = (params.get("resource") or "api-key").strip()
    if resource not in _RESOURCES:
        return SkillResult(
            ok=False, errors=[f"unknown resource {resource!r} — expected one of {list(_RESOURCES)}"]
        )
    key_id = (params.get("key_id") or "").strip()
    if not key_id:
        return SkillResult(ok=False, errors=["missing required argument: key_id"])
    site = (params.get("site") or "").strip() or None
    add, remove = _list(params.get("add_scope")), _list(params.get("remove_scope"))
    if not (site or add or remove):
        return SkillResult(ok=False, errors=[
            "nothing to change: pass --site, --add-scope or --remove-scope"])

    path = resolve_keys_path(params)
    if path is None:
        return SkillResult(ok=False, errors=[f"no keys file — pass --keys-file or set {KEYS_ENV}"])

    try:
        before, after = rescope_api_key_record(
            path, key_id, site=site, add_scopes=add, remove_scopes=remove)
    except (ApiKeysFileError, ValueError) as e:
        return SkillResult(ok=False, errors=[str(e)])

    changed = before != after
    summary = (
        f"rescoped API key {after['key_id']} ({after['principal']}): "
        f"site {before.get('site') or 'none'} -> {after.get('site') or 'none'}, "
        f"scopes {','.join(before['scopes'])} -> {','.join(after['scopes'])}; "
        "same token, effective on the next request"
        if changed else f"API key {after['key_id']} already has that site and scopes; unchanged"
    )
    return SkillResult(
        ok=True,
        value={
            "key_id": after["key_id"],
            "principal": after["principal"],
            "changed": changed,
            "before": {"site": before.get("site"), "scopes": list(before["scopes"])},
            "after": {"site": after.get("site"), "scopes": list(after["scopes"])},
            "keys_file": str(path),
        },
        actions_taken=[summary],
    )
