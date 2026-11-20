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


# -- how and when the person signed in (ADR-146) ------------------------------------


def test_a_session_records_when_the_person_signed_in():
    import time

    before = int(time.time())
    claims = verify_session_token(issue_session_token(_user(), issuer=ISS), issuer=ISS)
    assert before <= claims["auth_time"] <= int(time.time())


def test_a_session_records_how_the_person_signed_in():
    tok = issue_session_token(_user(), issuer=ISS, amr=("pwd",))
    assert verify_session_token(tok, issuer=ISS)["amr"] == ["pwd"]


def test_amr_is_absent_rather_than_guessed_when_the_caller_does_not_say():
    claims = verify_session_token(issue_session_token(_user(), issuer=ISS), issuer=ISS)
    assert "amr" not in claims


# --- a session follows its account ----------------------------------------------------


@pytest.fixture
def _accounts():
    from axiom.webauth.users import InMemoryUserStore, reset_user_store_for_tests, set_user_store

    store = InMemoryUserStore()
    set_user_store(store)
    yield store
    reset_user_store_for_tests()


def test_without_an_installed_store_a_session_is_the_token_alone():
    from axiom.webauth.users import installed_user_store, reset_user_store_for_tests

    reset_user_store_for_tests()
    assert installed_user_store() is None
    assert verify_session_token(issue_session_token(_user())) is not None


def test_a_removed_account_s_session_stops_working(_accounts):
    """A live node, 2026-10-31: the account was removed and its cookie still answered 200."""
    user = _user()
    _accounts.add(user)
    token = issue_session_token(user)
    assert verify_session_token(token) is not None
    _accounts._by_id.clear()
    _accounts._by_email.clear()
    assert verify_session_token(token) is None


def test_a_disabled_account_s_session_stops_working(_accounts):
    from dataclasses import replace

    user = _user()
    _accounts.add(user)
    token = issue_session_token(user)
    _accounts.add(replace(user, disabled=True))
    assert verify_session_token(token) is None


def test_an_sso_session_without_a_stored_account_still_works_until_disabled(_accounts):
    from dataclasses import replace

    sso = replace(_user(), attributes={"idp": "entra"})
    token = issue_session_token(sso)
    assert verify_session_token(token) is not None  # no stored account: sign-in did not provision one
    _accounts.add(replace(sso, disabled=True))
    assert verify_session_token(token) is None
