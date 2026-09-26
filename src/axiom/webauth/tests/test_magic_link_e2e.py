# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Magic-link END-TO-END across real components — not the pure core in isolation.

This exercises the actual chain a partner traverses: an admin invite → a mailed
link → the partner clicks it → a real ES256 session is minted from a real
:class:`User` and verified through the real JWT/key machinery → the session
carries their site. Plus the negative controls that make the checks able to fail,
and the user-empathy properties (zero extra input, a clear second-click result).

It composes magic_link + users + session + jwt + keys, so a break in any seam
between them shows up here even when each unit test still passes.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from axiom.webauth.keys import reset_key_store_for_tests
from axiom.webauth.magic_link import (
    InMemoryMagicLinkStore,
    MagicLinkError,
    issue_magic_link,
    redeem_magic_link,
)
from axiom.webauth.session import (
    issue_session_token,
    verify_session_token,
)
from axiom.webauth.users import InMemoryUserStore, User

ISS = "https://gate.node.example"  # generic: a consumer node name would leak into the public mirror
T0 = 1_000_000.0
TTL = 900.0


@pytest.fixture(autouse=True)
def _keys():
    reset_key_store_for_tests()
    yield
    reset_key_store_for_tests()


def _provision_user_from_link(identity, user_store):
    """JIT-provision (or fetch) the account a redeemed link proves — the step the
    webgate route will own. Site + roles come from the link (server-set), never
    from the visitor."""
    user_id = identity.email  # stable subject for a partner (pre-directory)
    existing = user_store.get_by_id(user_id)
    if existing is not None:
        return existing
    user = User(
        user_id=user_id, email=identity.email, site=identity.site,
        roles=identity.roles, password_hash=None,  # passwordless account
    )
    user_store.upsert(user)
    return user


# ------------------------------------------------------- the happy journey ----
def test_invite_to_signed_in_session_carries_the_bound_site():
    """ACU researcher: invited → clicks the link → is signed in, session scoped
    to acu-flowloop. The whole point: their site rides the credential."""
    links = InMemoryMagicLinkStore()
    users = InMemoryUserStore()

    # admin invites bob@acu.edu bound to acu-flowloop (site set server-side)
    token = issue_magic_link(
        "bob@acu.edu", "acu-flowloop", roles=("researcher",),
        ttl_seconds=TTL, now=T0, store=links,
    )
    # bob clicks — provides NOTHING but the click
    identity = redeem_magic_link(token, now=T0 + 30, store=links)
    user = _provision_user_from_link(identity, users)

    session = issue_session_token(user, issuer=ISS, ttl=timedelta(days=30))
    claims = verify_session_token(session, issuer=ISS)

    assert claims is not None, "a freshly minted session must verify"
    assert claims["email"] == "bob@acu.edu"
    assert claims["site"] == "acu-flowloop"          # site survived the whole chain
    assert "researcher" in claims["roles"]
    # the principal is site-scoped: whatever the handle-name sanitization, the
    # :context is the bound site (this is what per-site authz keys off).
    from axiom.webauth.users import principal_site
    assert principal_site(user.handle) == "acu-flowloop"


def test_two_different_partners_never_cross_sites():
    """Invites to two sites produce two sessions, each scoped to its own site —
    the isolation premise, proven end to end (not just in the model)."""
    links, users = InMemoryMagicLinkStore(), InMemoryUserStore()
    acu = redeem_magic_link(
        issue_magic_link("a@acu.edu", "acu-flowloop", ttl_seconds=TTL, now=T0, store=links),
        now=T0 + 1, store=links,
    )
    vcu = redeem_magic_link(
        issue_magic_link("v@vcu.edu", "vcu-flowloop", ttl_seconds=TTL, now=T0, store=links),
        now=T0 + 1, store=links,
    )
    a_claims = verify_session_token(
        issue_session_token(_provision_user_from_link(acu, users), issuer=ISS), issuer=ISS)
    v_claims = verify_session_token(
        issue_session_token(_provision_user_from_link(vcu, users), issuer=ISS), issuer=ISS)
    assert a_claims["site"] == "acu-flowloop"
    assert v_claims["site"] == "vcu-flowloop"
    assert a_claims["site"] != v_claims["site"]


# --------------------------------------------- negative controls (can fail) ---
def test_a_tampered_session_is_rejected():
    links, users = InMemoryMagicLinkStore(), InMemoryUserStore()
    identity = redeem_magic_link(
        issue_magic_link("b@acu.edu", "acu-flowloop", ttl_seconds=TTL, now=T0, store=links),
        now=T0 + 1, store=links,
    )
    session = issue_session_token(_provision_user_from_link(identity, users), issuer=ISS)
    tampered = session[:-3] + ("aaa" if not session.endswith("aaa") else "bbb")
    assert verify_session_token(tampered, issuer=ISS) is None


def test_an_expired_session_stops_verifying():
    """Prove the session TTL actually gates — mint already-expired, expect None.
    (If expiry were ignored, this returns claims and the test fails.)"""
    links, users = InMemoryMagicLinkStore(), InMemoryUserStore()
    identity = redeem_magic_link(
        issue_magic_link("c@acu.edu", "acu-flowloop", ttl_seconds=TTL, now=T0, store=links),
        now=T0 + 1, store=links,
    )
    user = _provision_user_from_link(identity, users)
    expired = issue_session_token(user, issuer=ISS, ttl=timedelta(seconds=-1))
    assert verify_session_token(expired, issuer=ISS) is None


# ---------------------------------------------------- user-empathy / friction -
def test_reclicking_the_email_link_gives_a_clean_single_use_error_not_a_crash():
    """A partner double-clicks or re-opens the email. They must get a clean,
    catchable 'already used' — never a traceback. (Low-friction failure mode.)"""
    links = InMemoryMagicLinkStore()
    token = issue_magic_link("d@acu.edu", "acu-flowloop", ttl_seconds=TTL, now=T0, store=links)
    redeem_magic_link(token, now=T0 + 5, store=links)
    with pytest.raises(MagicLinkError):   # a typed, handleable error — not KeyError/AttributeError
        redeem_magic_link(token, now=T0 + 6, store=links)


def test_partner_supplies_nothing_but_the_click():
    """Zero-friction property: everything the session needs (email, site, roles)
    comes from the invite, so redeeming requires no form, no site selection."""
    links, users = InMemoryMagicLinkStore(), InMemoryUserStore()
    token = issue_magic_link(
        "e@acu.edu", "acu-flowloop", roles=("researcher",), ttl_seconds=TTL, now=T0, store=links,
    )
    identity = redeem_magic_link(token, now=T0 + 1, store=links)   # only input = the token
    user = _provision_user_from_link(identity, users)
    claims = verify_session_token(issue_session_token(user, issuer=ISS), issuer=ISS)
    # the partner picked no site, filled no field — yet is correctly scoped
    assert claims["site"] == "acu-flowloop" and claims["email"] == "e@acu.edu"
