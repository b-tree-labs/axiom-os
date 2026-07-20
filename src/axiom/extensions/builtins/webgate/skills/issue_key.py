# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``gate.issue`` — issue a bearer API key for a NON-human API principal.

One verb replaces the plaintext-token-in-env provisioning dance: validate the
principal handle (``@name:context``) and scope grammar, mint an
auto-generated key id + high-entropy secret, store only the scrypt hash, and
print the plaintext token exactly once. The composed app's authz hook resolves
the key live (mtime hot-reload) — no restart, and revocation via
``gate.revoke`` is immediate.

Scopes are ``<mount>[:<verb>]`` — the mounts of the composed HTTP app this
key may reach, optionally narrowed to a governance verb (``read`` / ``invoke``
/ ``access``). Enforcement is fail-closed in the authz hook.
"""

from __future__ import annotations

from typing import Any

from axiom.extensions.builtins.http.authz_hook import parse_scope
from axiom.infra.skills import SkillContext, SkillResult
from axiom.webauth import ApiKeysFileError, append_api_key_record, mint_api_key

from ._accounts import KEYS_ENV, resolve_keys_path

_RESOURCES = ("api-key",)


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    resource = (params.get("resource") or "api-key").strip()
    if resource not in _RESOURCES:
        return SkillResult(ok=False, errors=[
            f"unknown resource {resource!r} — expected one of {list(_RESOURCES)}"])

    principal = (params.get("principal") or "").strip()
    if not principal:
        return SkillResult(ok=False, errors=[
            "missing required argument: principal (e.g. @svc:context)"])

    scopes = tuple(params.get("scope") or ())
    if not scopes:
        return SkillResult(ok=False, errors=[
            "at least one scope is required (repeatable --scope MOUNT[:VERB]); "
            "issuance is least-privilege — there is no default grant"])
    for s in scopes:
        try:
            parse_scope(s)
        except ValueError as e:
            return SkillResult(ok=False, errors=[f"invalid scope: {e}"])

    path = resolve_keys_path(params)
    if path is None:
        return SkillResult(ok=False, errors=[
            f"no keys file — pass --keys-file or set {KEYS_ENV}"])

    try:
        token, record = mint_api_key(principal=principal, scopes=scopes,
                                     name=params.get("name") or "")
        append_api_key_record(path, record)
    except (ApiKeysFileError, ValueError) as e:
        return SkillResult(ok=False, errors=[str(e)])

    scope_str = ", ".join(record["scopes"])
    return SkillResult(
        ok=True,
        value={
            "key_id": record["key_id"],
            "principal": record["principal"],
            "scopes": record["scopes"],
            "name": record["name"],
            "created_at": record["created_at"],
            "keys_file": str(path),
            "token": token,
        },
        actions_taken=[
            f"issued API key {record['key_id']} for {record['principal']} "
            f"— scopes: {scope_str} → {path}",
            f"API key (shown once — relay it securely; it is hashed at rest): "
            f"{token}",
        ],
    )
