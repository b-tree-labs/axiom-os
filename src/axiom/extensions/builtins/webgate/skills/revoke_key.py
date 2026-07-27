# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``gate.revoke`` — revoke an issued API key, effective immediately.

Sets ``revoked_at`` on the record; the atomic rewrite bumps the file mtime
the authz hook's store watches, so the very next request presenting the key
is denied — no restart, no cache to expire. Idempotent: revoking an
already-revoked key reports the original revocation time.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult
from axiom.webauth import ApiKeysFileError, revoke_api_key_record

from ._accounts import KEYS_ENV, resolve_keys_path

_RESOURCES = ("api-key",)


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    resource = (params.get("resource") or "api-key").strip()
    if resource not in _RESOURCES:
        return SkillResult(ok=False, errors=[
            f"unknown resource {resource!r} — expected one of {list(_RESOURCES)}"])

    key_id = (params.get("key_id") or "").strip()
    if not key_id:
        return SkillResult(ok=False, errors=["missing required argument: key_id"])

    path = resolve_keys_path(params)
    if path is None:
        return SkillResult(ok=False, errors=[
            f"no keys file — pass --keys-file or set {KEYS_ENV}"])

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
