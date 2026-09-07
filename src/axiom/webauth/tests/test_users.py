# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""User accounts + password authentication (shared by the web gate and OIDC)."""

from __future__ import annotations

import pytest

from axiom.webauth.password import get_password_hash
from axiom.webauth.users import (
    InMemoryUserStore,
    User,
    authenticate,
    reset_user_store_for_tests,
)

PW = "correct-horse-battery-staple"


@pytest.fixture(autouse=True)
def _reset():
    reset_user_store_for_tests()
    yield
    reset_user_store_for_tests()


def _store():
    return InMemoryUserStore([
        User(user_id="u1", email="Alice@Example.org",
             password_hash=get_password_hash(PW), name="Alice", roles=("user",)),
        User(user_id="u2", email="bob@example.org",
             password_hash=get_password_hash("bobpw-000000000000"), disabled=True),
    ])


def test_lookup_is_case_insensitive_on_email():
    store = _store()
    assert store.get_by_email("alice@example.org").user_id == "u1"
    assert store.get_by_email("ALICE@EXAMPLE.ORG").user_id == "u1"
    assert store.get_by_id("u1").email == "alice@example.org"  # normalized-stored


def test_authenticate_success_returns_user():
    user = authenticate(_store(), "alice@example.org", PW)
    assert user is not None and user.user_id == "u1"


def test_authenticate_wrong_password_fails():
    assert authenticate(_store(), "alice@example.org", "nope") is None


def test_authenticate_unknown_user_fails():
    assert authenticate(_store(), "ghost@example.org", PW) is None


def test_disabled_user_cannot_authenticate():
    assert authenticate(_store(), "bob@example.org", "bobpw-000000000000") is None


def test_passwordless_user_cannot_password_authenticate():
    # An SSO-only account (no password hash) must never pass password auth.
    store = InMemoryUserStore([User(user_id="s", email="sso@example.org", password_hash=None)])
    assert authenticate(store, "sso@example.org", "") is None
    assert authenticate(store, "sso@example.org", "anything") is None


def test_process_wide_store_get_set():
    from axiom.webauth.users import get_user_store, set_user_store
    set_user_store(_store())
    assert get_user_store().get_by_email("alice@example.org") is not None
