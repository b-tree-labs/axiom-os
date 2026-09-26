# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Rotating refresh tokens with reuse detection (ADR-085).

Refresh tokens are **opaque and single-use**: every refresh consumes the presented
token and issues a fresh one in the same *family*. If a token that was already
rotated away is presented again, that is the signature of a stolen token being
replayed — so the entire family is revoked, logging out the thief and the victim
alike (the victim re-authenticates; the thief is locked out). This is the
OAuth 2.1 recommended defence for public clients that cannot keep a secret.

This cut ships an in-memory store; the Postgres-backed store over
``axiom.infra.db.session_for("oauth")`` follows — durability matters here because
the ``_consumed`` set is what makes reuse detection survive a restart.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

# Refresh tokens outlive access tokens; the store default mirrors webauth's
# REFRESH_TOKEN_EXPIRE_DAYS without importing it (kept a plain seconds value).
_DEFAULT_TTL_SECONDS = 30 * 24 * 3600


@dataclass(frozen=True)
class RefreshToken:
    """An opaque refresh token and the grant it can re-mint."""

    token: str
    family_id: str
    client_id: str
    subject: str
    scope: str
    resource: str | None
    expires_at: float

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at


class RefreshTokenStore(Protocol):
    """Issue rotating refresh tokens and rotate (consume) them with reuse detection."""

    def issue(
        self,
        *,
        client_id: str,
        subject: str,
        scope: str,
        resource: str | None = None,
        family_id: str | None = None,
    ) -> RefreshToken: ...

    def rotate(self, token: str) -> RefreshToken | None: ...


class InMemoryRefreshTokenStore:
    """A dict-backed rotating store with family reuse detection."""

    def __init__(
        self,
        *,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._active: dict[str, RefreshToken] = {}
        self._consumed: dict[str, str] = {}  # token -> family_id (rotated-away)
        self._revoked_families: set[str] = set()
        self._ttl = ttl_seconds
        self._now = now

    def issue(
        self,
        *,
        client_id: str,
        subject: str,
        scope: str,
        resource: str | None = None,
        family_id: str | None = None,
    ) -> RefreshToken:
        record = RefreshToken(
            token=secrets.token_urlsafe(32),
            family_id=family_id or secrets.token_urlsafe(16),
            client_id=client_id,
            subject=subject,
            scope=scope,
            resource=resource,
            expires_at=self._now() + self._ttl,
        )
        self._active[record.token] = record
        return record

    def rotate(self, token: str) -> RefreshToken | None:
        """Consume ``token`` and return its record, or ``None`` if it cannot be used.

        A replayed (already-consumed) token revokes its whole family as a side
        effect — the theft signal. Unknown / expired / revoked tokens are a plain
        ``None`` (the caller maps that to ``invalid_grant``).
        """
        if token in self._consumed:
            self._revoke_family(self._consumed[token])
            return None

        record = self._active.get(token)
        if record is None or record.family_id in self._revoked_families:
            return None
        if record.is_expired(self._now()):
            del self._active[token]
            return None

        # Consume: the token is spent, and remembered so a replay is detectable.
        del self._active[token]
        self._consumed[token] = record.family_id
        return record

    def _revoke_family(self, family_id: str) -> None:
        self._revoked_families.add(family_id)
        for tok in [t for t, r in self._active.items() if r.family_id == family_id]:
            del self._active[tok]


_STORE: RefreshTokenStore | None = None


def get_refresh_token_store() -> RefreshTokenStore:
    """The process-wide refresh-token store (cached in-memory default)."""
    global _STORE
    if _STORE is None:
        _STORE = InMemoryRefreshTokenStore()
    return _STORE


def set_refresh_token_store(store: RefreshTokenStore) -> None:
    global _STORE
    _STORE = store


def reset_refresh_token_store_for_tests() -> None:
    global _STORE
    _STORE = None


__all__ = [
    "InMemoryRefreshTokenStore",
    "RefreshToken",
    "RefreshTokenStore",
    "get_refresh_token_store",
    "reset_refresh_token_store_for_tests",
    "set_refresh_token_store",
]
