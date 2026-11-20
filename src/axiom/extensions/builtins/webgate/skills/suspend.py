# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``gate.suspend`` / ``gate.reinstate`` — stop a person at once, and let them back in place.

Suspending ends every session and refuses every key the person holds on the next
request (MVP-14-A3). Nothing is revoked and nothing is re-issued: the gate reads
the account live on each request (``axiom.webauth.live``), so resuming makes the
same browser session and the same keys work again. That is the lifecycle rule:
a person is never made to sign in again or fetch a new key because an
administrator changed their state.

The mark lives in the account's attributes (``suspended_at``, ``suspended_by``,
``suspended_reason``), which every release reads, so rolling back a release never
locks the accounts file.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult
from axiom.webauth import AccountsFileError, load_user_records, upsert_user_record
from axiom.webauth.live import SUSPENDED_AT, SUSPENDED_BY, SUSPENDED_REASON

from ._accounts import ACCOUNTS_ENV, resolve_accounts_path, resolve_keys_path


def _target(params: dict[str, Any]) -> tuple[Any, dict | None, SkillResult | None]:
    email = (params.get("email") or "").strip().lower()
    if not email:
        return None, None, SkillResult(ok=False, errors=["name the account: email is required"])
    path = resolve_accounts_path(params)
    if path is None:
        return None, None, SkillResult(
            ok=False, errors=[f"no accounts file: pass --accounts-file or set {ACCOUNTS_ENV}"])
    if not path.is_file():
        return None, None, SkillResult(ok=False, errors=[f"accounts file not found: {path}"])
    try:
        records = load_user_records(path)
    except AccountsFileError as e:
        return None, None, SkillResult(ok=False, errors=[str(e)])
    rec = next((r for r in records if r["email"] == email), None)
    if rec is None:
        return path, None, SkillResult(ok=False, errors=[f"no such account: {email}"])
    return path, rec, None


def _holders_keys(params: dict[str, Any]) -> list[str]:
    """The live keys this account holds, by the same rule the key store applies."""
    from axiom.webauth import load_api_key_records
    from axiom.webauth.live import key_holder_suspended

    keys = resolve_keys_path(params)
    if keys is None or not keys.is_file():
        return []
    try:
        return [r["key_id"] for r in load_api_key_records(keys)
                if not r.get("revoked_at") and key_holder_suspended(r)]
    except Exception:  # noqa: BLE001 - the count is a courtesy; the refusal does not depend on it
        return []


def suspend(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    path, rec, refusal = _target(params)
    if refusal:
        return refusal
    attrs = dict(rec.get("attributes") or {})
    if attrs.get(SUSPENDED_AT):
        return SkillResult(
            ok=True,
            value={"email": rec["email"], "suspended": True, "since": attrs[SUSPENDED_AT],
                   "keys_refused": _holders_keys(params)},
            actions_taken=[f"{rec['email']} was already suspended (since {attrs[SUSPENDED_AT]})"],
        )
    attrs[SUSPENDED_AT] = datetime.now(UTC).isoformat(timespec="seconds")
    by = str(params.get("by") or getattr(ctx, "actor", "") or "").strip()
    if by:
        attrs[SUSPENDED_BY] = by
    reason = str(params.get("reason") or "").strip()
    if reason:
        attrs[SUSPENDED_REASON] = reason
    try:
        upsert_user_record(path, email=rec["email"], password_hash=None, attributes=attrs,
                           overwrite=True)
    except AccountsFileError as e:
        return SkillResult(ok=False, errors=[str(e)])
    keys = _holders_keys(params)
    return SkillResult(
        ok=True,
        value={"email": rec["email"], "suspended": True, "since": attrs[SUSPENDED_AT],
               "keys_refused": keys},
        actions_taken=[
            f"{rec['email']} suspended: every session ends and {len(keys)} key(s) are refused "
            f"from the next request. Nothing was revoked; `gate reinstate` lets the same "
            f"session and keys back in."
        ],
    )


def reinstate(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    del ctx
    path, rec, refusal = _target(params)
    if refusal:
        return refusal
    attrs = dict(rec.get("attributes") or {})
    if not attrs.get(SUSPENDED_AT):
        return SkillResult(ok=True, value={"email": rec["email"], "suspended": False},
                           actions_taken=[f"{rec['email']} was not suspended"])
    for k in (SUSPENDED_AT, SUSPENDED_BY, SUSPENDED_REASON):
        attrs.pop(k, None)
    try:
        upsert_user_record(path, email=rec["email"], password_hash=None, attributes=attrs,
                           overwrite=True)
    except AccountsFileError as e:
        return SkillResult(ok=False, errors=[str(e)])
    return SkillResult(
        ok=True,
        value={"email": rec["email"], "suspended": False},
        actions_taken=[f"{rec['email']} reinstated: the same session and keys work again"],
    )


__all__ = ["reinstate", "suspend"]
