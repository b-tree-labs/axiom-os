# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""User accounts + password authentication.

The account model shared by the browser-session web gate (``webgate``) and, as
the OIDC fast-follow, the ``oauth`` authorization server — one user store, one
set of credentials, so adding OIDC does not fork identity. Passwords are scrypt
hashes (:mod:`axiom.webauth.password`); a ``None`` hash marks an SSO-only account
that can never pass password auth.

This cut ships an in-memory store (config/env provisioning); a Postgres-backed
store over ``axiom.infra.db.session_for`` follows.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .password import verify_password


def _norm_email(email: str) -> str:
    return email.strip().lower()


@dataclass(frozen=True)
class User:
    """A user account. ``user_id`` is the stable principal subject."""

    user_id: str
    email: str
    password_hash: str | None = None
    name: str = ""
    roles: tuple[str, ...] = ()
    disabled: bool = False
    attributes: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Store the email normalized so lookup and the token 'email' claim agree.
        object.__setattr__(self, "email", _norm_email(self.email))


@runtime_checkable
class UserStore(Protocol):
    """Lookup of accounts by email or id. ``None`` when unknown."""

    def get_by_email(self, email: str) -> User | None: ...
    def get_by_id(self, user_id: str) -> User | None: ...


class InMemoryUserStore:
    """A dict-backed user store (config-provisioned accounts + tests)."""

    def __init__(self, users: Iterable[User] = ()) -> None:
        self._by_email: dict[str, User] = {}
        self._by_id: dict[str, User] = {}
        for user in users:
            self.add(user)

    def add(self, user: User) -> None:
        self._by_email[_norm_email(user.email)] = user
        self._by_id[user.user_id] = user

    def get_by_email(self, email: str) -> User | None:
        return self._by_email.get(_norm_email(email))

    def get_by_id(self, user_id: str) -> User | None:
        return self._by_id.get(user_id)

    def __len__(self) -> int:
        return len(self._by_id)


def authenticate(store: UserStore, email: str, password: str) -> User | None:
    """Return the user iff the password is correct and the account is usable.

    Fails closed on unknown / disabled / passwordless (SSO-only) accounts.
    """
    user = store.get_by_email(email)
    if user is None or user.disabled or user.password_hash is None:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


_STORE: UserStore | None = None


def get_user_store() -> UserStore:
    """The process-wide user store (empty in-memory default — fail-closed)."""
    global _STORE
    if _STORE is None:
        _STORE = InMemoryUserStore()
    return _STORE


def set_user_store(store: UserStore) -> None:
    global _STORE
    _STORE = store


def reset_user_store_for_tests() -> None:
    global _STORE
    _STORE = None


__all__ = [
    "InMemoryUserStore",
    "User",
    "UserStore",
    "authenticate",
    "get_user_store",
    "reset_user_store_for_tests",
    "set_user_store",
]
