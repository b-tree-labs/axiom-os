# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Bearer API keys for NON-human API principals — issuance, storage, resolve.

The web gate authenticates *humans* (password accounts, sessions). This module
is its sibling for *services*: a key file holds bearer credentials bound to
service principals (``@name:context``) with scopes, so the composed HTTP app's
authz hook can resolve ``Authorization: Bearer axk_…`` to a principal without
any human in the loop.

Design mirrors :mod:`axiom.webauth.file_store` deliberately:

* **Hashed at rest** — the file stores a self-describing scrypt hash (the same
  :func:`axiom.webauth.password.get_password_hash` scheme the gate's password
  accounts use); the plaintext token is shown exactly once at issuance.
* **Fail-closed validation** — :func:`load_api_key_records` rejects the whole
  file on any problem (plaintext secret, unknown field, duplicate/missing ids,
  invalid principal handle, empty scopes). Partial key sets never load.
* **Hot-reload + immediate revocation** — :class:`JsonFileApiKeyStore` stats
  the file per lookup and re-parses on mtime change, so ``gate.revoke`` takes
  effect on the next request with no restart. A parse failure fails closed to
  deny-all.

Token shape: ``axk_<key_id>_<secret>``. The embedded, auto-generated ``key_id``
makes resolution O(1) (find the record, then one scrypt verify); a bounded
per-key verification cache keeps the hot path off scrypt while revocation and
scope state are still read fresh from the (mtime-cached) file on every call.

Scope *strings* are opaque here — the HTTP authz hook owns the grammar
(``<mount>[:<verb>]``) and its enforcement; this module only guarantees each
record carries a non-empty list of non-empty strings.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Sequence

from axiom.vega.identity.principal import Principal

from .password import get_password_hash, verify_password

TOKEN_PREFIX = "axk_"
"""Every issued key starts with this — the authz hook routes on it."""

_KEY_FIELDS = frozenset({
    "key_id", "principal", "secret_hash", "scopes", "name",
    "created_at", "revoked_at",
})


class ApiKeysFileError(ValueError):
    """The API-keys file is missing or invalid (fail-closed)."""


@dataclass(frozen=True)
class ApiKeyIdentity:
    """What a valid presented token resolves to."""

    key_id: str
    principal: str
    scopes: tuple[str, ...]
    name: str = ""


# ---------------------------------------------------------------------------
# Minting + token shape
# ---------------------------------------------------------------------------


def parse_token(token: str) -> tuple[str, str] | None:
    """Split ``axk_<key_id>_<secret>`` → ``(key_id, secret)``; else ``None``.

    The secret may itself contain separators — only the first ``_`` after the
    key id splits.
    """
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    rest = token[len(TOKEN_PREFIX):]
    key_id, sep, secret = rest.partition("_")
    if not sep or not key_id or not secret:
        return None
    return key_id, secret


def _validate_principal(handle: str) -> str:
    """Validate ``@name:context`` via the Principal grammar; return it."""
    handle = (handle or "").strip()
    # Principal.__post_init__ enforces the single-@ matrix-style grammar.
    Principal(handle=handle, public_bytes=b"\x00" * 32)
    return handle


def _validate_scopes(scopes: Sequence[str]) -> tuple[str, ...]:
    out = tuple(str(s).strip() for s in scopes or ())
    if not out or any(not s for s in out):
        raise ValueError("scopes must be a non-empty list of non-empty strings")
    return out


def mint_api_key(
    *,
    principal: str,
    scopes: Sequence[str],
    name: str = "",
) -> tuple[str, dict]:
    """Mint a key: returns ``(plaintext_token, record)``.

    The record stores only the scrypt hash of the token; the plaintext exists
    solely in the return value — show it once and drop it. ``key_id`` is
    auto-generated (callers never invent identifiers on create).
    """
    handle = _validate_principal(principal)
    scope_list = _validate_scopes(scopes)
    key_id = uuid.uuid4().hex[:12]
    secret = secrets.token_urlsafe(32)
    token = f"{TOKEN_PREFIX}{key_id}_{secret}"
    record = {
        "key_id": key_id,
        "principal": handle,
        "secret_hash": get_password_hash(token),
        "scopes": list(scope_list),
        "name": str(name or ""),
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "revoked_at": None,
    }
    return token, record


# ---------------------------------------------------------------------------
# File I/O (fail-closed, atomic — mirrors file_store.py)
# ---------------------------------------------------------------------------


def _validate_records(raw: object, *, where: str) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise ApiKeysFileError(f"{where}: top level must be a list of key objects")

    records: list[dict] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        at = f"{where}[{i}]"
        if not isinstance(entry, dict):
            raise ApiKeysFileError(f"{at}: each key must be an object")
        for banned in ("secret", "token"):
            if banned in entry:
                raise ApiKeysFileError(
                    f"{at}: {banned!r} (plaintext) is not allowed — keys are "
                    "hashed at rest; store 'secret_hash' only"
                )
        unknown = set(entry) - _KEY_FIELDS
        if unknown:
            raise ApiKeysFileError(
                f"{at}: unknown field(s): {', '.join(sorted(unknown))}")
        key_id = str(entry.get("key_id", "")).strip()
        if not key_id:
            raise ApiKeysFileError(f"{at}: 'key_id' is required")
        if key_id in seen:
            raise ApiKeysFileError(f"{at}: duplicate key_id {key_id!r}")
        seen.add(key_id)
        secret_hash = entry.get("secret_hash")
        if not (isinstance(secret_hash, str) and secret_hash.strip()):
            raise ApiKeysFileError(f"{at}: 'secret_hash' is required (non-empty)")
        try:
            principal = _validate_principal(str(entry.get("principal", "")))
        except ValueError as e:
            raise ApiKeysFileError(f"{at}: invalid principal: {e}") from e
        scopes = entry.get("scopes")
        if isinstance(scopes, str) or not isinstance(scopes, (list, tuple)):
            raise ApiKeysFileError(f"{at}: 'scopes' must be a list of strings")
        try:
            scope_list = _validate_scopes(scopes)
        except ValueError as e:
            raise ApiKeysFileError(f"{at}: invalid scopes: {e}") from e
        revoked_at = entry.get("revoked_at")
        if revoked_at is not None and not isinstance(revoked_at, str):
            raise ApiKeysFileError(f"{at}: 'revoked_at' must be a string or null")
        records.append({
            "key_id": key_id,
            "principal": principal,
            "secret_hash": secret_hash,
            "scopes": list(scope_list),
            "name": str(entry.get("name", "") or ""),
            "created_at": str(entry.get("created_at", "") or ""),
            "revoked_at": revoked_at,
        })
    return records


def load_api_key_records(path: str | os.PathLike) -> list[dict]:
    """Read + validate the keys file. Fail-closed: any problem raises."""
    p = Path(path)
    if not p.is_file():
        raise ApiKeysFileError(f"API-keys file not found: {p}")
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise ApiKeysFileError(f"could not parse {p}: {e}") from e
    return _validate_records(raw, where=str(p))


def save_api_key_records(path: str | os.PathLike, records: Iterable[dict]) -> Path:
    """Validate + atomically write the keys file (mode 0600).

    Validation happens before any write; temp-then-``os.replace`` so a reader
    never observes a half-written file and the rename bumps the watched mtime.
    """
    p = Path(path)
    validated = _validate_records(list(records), where=str(p))
    payload = json.dumps(validated, indent=2) + "\n"
    tmp = p.with_name(p.name + ".tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    finally:
        if tmp.exists():
            tmp.unlink()
    return p


def append_api_key_record(path: str | os.PathLike, record: dict) -> dict:
    """Append one freshly minted record, preserving the rest of the file."""
    p = Path(path)
    records = load_api_key_records(p) if p.is_file() else []
    key_id = str(record.get("key_id", "")).strip()
    if any(r["key_id"] == key_id for r in records):
        raise ApiKeysFileError(f"key already exists: {key_id}")
    records.append(record)
    save_api_key_records(p, records)
    return record


def revoke_api_key_record(path: str | os.PathLike, key_id: str) -> dict:
    """Set ``revoked_at`` on one key (idempotent). Unknown id raises.

    Revocation is immediate for any :class:`JsonFileApiKeyStore` watching the
    file — the atomic rewrite bumps the mtime it reloads on.
    """
    p = Path(path)
    records = load_api_key_records(p)
    for r in records:
        if r["key_id"] == key_id:
            if not r.get("revoked_at"):
                r["revoked_at"] = datetime.now(UTC).isoformat(timespec="seconds")
                save_api_key_records(p, records)
            return r
    raise ApiKeysFileError(f"no such key: {key_id}")


# ---------------------------------------------------------------------------
# The live store the authz hook reads
# ---------------------------------------------------------------------------


class JsonFileApiKeyStore:
    """Resolve presented tokens against the keys file, hot-reloaded on change.

    Per lookup: cheap ``stat``; re-parse only on mtime change; fail closed to
    deny-all on a missing/broken file (retrying when it is edited again).
    Revocation and scope changes are therefore immediate — every ``resolve``
    reads current record state.

    A per-key verification cache (``key_id`` → sha256 of the last token that
    passed scrypt) keeps repeated requests off the memory-hard hash; the cache
    is dropped whenever the file reloads, so a rotated/edited record can never
    be satisfied by a stale verification.
    """

    def __init__(self, path: str | os.PathLike | None) -> None:
        self._path = Path(path) if path else None
        self._mtime: float | None = None
        self._records: dict[str, dict] = {}
        self._verified: dict[str, str] = {}

    def _refresh(self) -> None:
        if not self._path:
            self._records = {}
            return
        try:
            mt = self._path.stat().st_mtime
        except OSError:
            self._records = {}
            self._verified = {}
            self._mtime = None
            return
        if mt == self._mtime:
            return
        self._mtime = mt
        self._verified = {}
        try:
            records = load_api_key_records(self._path)
        except Exception:
            self._records = {}  # fail-closed: deny-all until fixed
            return
        self._records = {r["key_id"]: r for r in records}

    def reload(self) -> None:
        """Force a re-read on the next lookup (bypass the mtime cache)."""
        self._mtime = None

    def get(self, key_id: str) -> dict | None:
        self._refresh()
        return self._records.get(key_id)

    def resolve(self, token: str) -> ApiKeyIdentity | None:
        """A presented bearer token → its identity, or ``None`` (deny).

        ``None`` for: unparseable token, unknown id, revoked key, or a secret
        that fails verification. Never raises.
        """
        parsed = parse_token(token)
        if parsed is None:
            return None
        key_id, _secret = parsed
        record = self.get(key_id)
        if record is None or record.get("revoked_at"):
            return None
        fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if self._verified.get(key_id) != fingerprint:
            if not verify_password(token, record["secret_hash"]):
                return None
            self._verified[key_id] = fingerprint
        return ApiKeyIdentity(
            key_id=key_id,
            principal=record["principal"],
            scopes=tuple(record["scopes"]),
            name=record.get("name", ""),
        )

    def __len__(self) -> int:
        self._refresh()
        return len(self._records)


__all__ = [
    "ApiKeyIdentity",
    "ApiKeysFileError",
    "JsonFileApiKeyStore",
    "TOKEN_PREFIX",
    "append_api_key_record",
    "load_api_key_records",
    "mint_api_key",
    "parse_token",
    "revoke_api_key_record",
    "save_api_key_records",
]
