# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Vault-keyed refresh-token custody (AUTH-4, AUTH-R3/R4).

Refresh tokens are persisted only through a secure ``TokenStore`` (default: the
OS keychain), keyed by ``(provider, user, scopes)`` — never on disk in the clear,
never logged. ``token_source(...)`` reads the stored refresh token and returns
the always-fresh access-token callable a connector consumes.
"""

from __future__ import annotations

import hashlib
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from axiom.extensions.builtins.auth.providers import IdpConfig
from axiom.extensions.builtins.auth.token_source import TokenSource


@runtime_checkable
class TokenStore(Protocol):
    def get(self, key: str) -> Optional[str]: ...

    def put(self, key: str, value: str) -> None: ...


class InMemoryTokenStore:
    """Ephemeral store — tests / throwaway nodes."""

    def __init__(self) -> None:
        self._d: dict[str, str] = {}

    def get(self, key: str) -> Optional[str]:
        return self._d.get(key)

    def put(self, key: str, value: str) -> None:
        self._d[key] = value


class KeychainTokenStore:
    """OS-keychain-backed store (via setup.secrets) — never on disk in clear."""

    def get(self, key: str) -> Optional[str]:
        from axiom.setup.secrets import get_secret

        return get_secret(key)

    def put(self, key: str, value: str) -> None:
        from axiom.setup.secrets import store_secret

        store_secret(key, value)


def token_key(provider: str, user: str, scopes: list) -> str:
    """Stable key for a (provider, user, scope-set) — scope order-independent."""
    digest = hashlib.sha256(" ".join(sorted(scopes)).encode()).hexdigest()[:12]
    return f"axiom.auth.{provider}.{user}.{digest}"


def store_refresh_token(
    provider: str, user: str, scopes: list, refresh_token: str, *, store: Optional[TokenStore] = None
) -> None:
    (store or KeychainTokenStore()).put(token_key(provider, user, scopes), refresh_token)


def load_refresh_token(
    provider: str, user: str, scopes: list, *, store: Optional[TokenStore] = None
) -> Optional[str]:
    return (store or KeychainTokenStore()).get(token_key(provider, user, scopes))


def token_source(
    *,
    provider: IdpConfig,
    user: str,
    scopes: list,
    http: Any,
    client_id: str,
    client_secret: Optional[str] = None,
    store: Optional[TokenStore] = None,
    now: Optional[Callable[[], float]] = None,
) -> Callable[[], str]:
    """The always-fresh access-token callable for ``(provider, user, scopes)``,
    backed by the stored refresh token. Raises if the user hasn't logged in."""
    refresh = load_refresh_token(provider.name, user, scopes, store=store)
    if refresh is None:
        raise LookupError(
            f"no stored refresh token for {user}@{provider.name}; run `axi auth login` first"
        )
    return TokenSource(
        http, provider, client_id=client_id, refresh_token=refresh,
        client_secret=client_secret, scopes=scopes, now=now,
    )


__all__ = [
    "InMemoryTokenStore",
    "KeychainTokenStore",
    "TokenStore",
    "load_refresh_token",
    "store_refresh_token",
    "token_key",
    "token_source",
]
