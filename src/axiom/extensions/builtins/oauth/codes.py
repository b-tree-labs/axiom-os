# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The authorization-code store (ADR-082).

An authorization code is a short-lived, single-use bearer of everything the token
exchange must re-verify: which client and redirect_uri it was issued to, the
authenticated resource owner, the granted scope + resource, and the PKCE
challenge. This cut ships an in-memory store; a Postgres-backed store over
``axiom.infra.db.session_for("oauth")`` follows (codes must survive nothing —
they live seconds — but the store also anchors refresh-token reuse detection).

Single-use is enforced by *removing* the code on the first ``consume`` (even when
expired), so a replayed code never redeems twice.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AuthorizationCode:
    """A redeemed-once authorization code and its bound context."""

    code: str
    client_id: str
    redirect_uri: str
    subject: str
    scope: str
    code_challenge: str
    code_challenge_method: str
    resource: str | None
    expires_at: float

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at


class AuthorizationCodeStore(Protocol):
    """Issue and redeem authorization codes."""

    def issue(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        subject: str,
        scope: str,
        code_challenge: str,
        code_challenge_method: str,
        resource: str | None = None,
    ) -> AuthorizationCode: ...

    def consume(self, code: str) -> AuthorizationCode | None: ...


class InMemoryAuthorizationCodeStore:
    """A dict-backed code store. Codes live ``ttl_seconds`` (default 60)."""

    def __init__(
        self, *, ttl_seconds: int = 60, now: Callable[[], float] = time.time
    ) -> None:
        self._codes: dict[str, AuthorizationCode] = {}
        self._ttl = ttl_seconds
        self._now = now

    def issue(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        subject: str,
        scope: str,
        code_challenge: str,
        code_challenge_method: str,
        resource: str | None = None,
    ) -> AuthorizationCode:
        record = AuthorizationCode(
            code=secrets.token_urlsafe(32),
            client_id=client_id,
            redirect_uri=redirect_uri,
            subject=subject,
            scope=scope,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            resource=resource,
            expires_at=self._now() + self._ttl,
        )
        self._codes[record.code] = record
        return record

    def consume(self, code: str) -> AuthorizationCode | None:
        # Pop first: a code is spent the moment it is presented, so a replay
        # (or a presentation after expiry) finds nothing.
        record = self._codes.pop(code, None)
        if record is None or record.is_expired(self._now()):
            return None
        return record


_STORE: AuthorizationCodeStore | None = None


def get_authorization_code_store() -> AuthorizationCodeStore:
    """The process-wide authorization-code store (cached in-memory default)."""
    global _STORE
    if _STORE is None:
        _STORE = InMemoryAuthorizationCodeStore()
    return _STORE


def set_authorization_code_store(store: AuthorizationCodeStore) -> None:
    """Install the process-wide code store (deployment wiring / tests)."""
    global _STORE
    _STORE = store


def reset_authorization_code_store_for_tests() -> None:
    global _STORE
    _STORE = None


__all__ = [
    "AuthorizationCode",
    "AuthorizationCodeStore",
    "InMemoryAuthorizationCodeStore",
    "get_authorization_code_store",
    "reset_authorization_code_store_for_tests",
    "set_authorization_code_store",
]
