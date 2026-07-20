# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Browser session tokens (ES256 cookie value) — shared by the gate and OIDC."""

from __future__ import annotations

from datetime import timedelta

import pytest

from axiom.webauth.keys import reset_key_store_for_tests
from axiom.webauth.session import (
    SESSION_COOKIE,
    issue_session_token,
    session_from_cookies,
    verify_session_token,
)
from axiom.webauth.users import User

ISS = "https://gate.example"


@pytest.fixture(autouse=True)
def _keys():
    reset_key_store_for_tests()
    yield
    reset_key_store_for_tests()


def _user():
    return User(user_id="u1", email="alice@example.org", name="Alice", roles=("user", "op"))


def test_round_trip_carries_identity():
    tok = issue_session_token(_user(), issuer=ISS)
    claims = verify_session_token(tok, issuer=ISS)
    assert claims is not None
    assert claims["sub"] == "u1"
    assert claims["email"] == "alice@example.org"
    assert claims["name"] == "Alice"
    assert claims["type"] == "session"


def test_access_token_is_not_accepted_as_a_session():
    # A plain access token (type=access) must not pass as a session cookie.
    from axiom.webauth.jwt import create_access_token
    at = create_access_token({"sub": "u1"}, issuer=ISS)
    assert verify_session_token(at, issuer=ISS) is None


def test_expired_session_rejected():
    tok = issue_session_token(_user(), ttl=timedelta(seconds=-1), issuer=ISS)
    assert verify_session_token(tok, issuer=ISS) is None


def test_wrong_issuer_rejected():
    tok = issue_session_token(_user(), issuer=ISS)
    assert verify_session_token(tok, issuer="https://evil.example") is None


def test_session_from_cookies_reads_the_named_cookie():
    tok = issue_session_token(_user(), issuer=ISS)
    assert session_from_cookies({SESSION_COOKIE: tok}, issuer=ISS)["sub"] == "u1"
    assert session_from_cookies({}, issuer=ISS) is None
    assert session_from_cookies({SESSION_COOKIE: "garbage"}, issuer=ISS) is None
