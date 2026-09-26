# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Magic-link sign-in core — issue a single-use, expiring, email-verified link
bound to a site, then redeem it once. Audit properties under test: hashed at
rest, single-use, expiring, site-bound server-side, tamper-rejecting.

Transport (email) and durable storage are injected — this is the pure core the
webgate route and a Postgres store build on.
"""

from __future__ import annotations

import pytest

from axiom.webauth.magic_link import (
    InMemoryMagicLinkStore,
    MagicLinkError,
    issue_magic_link,
    redeem_magic_link,
    split_magic_token,
)

T0 = 1_000_000.0
TTL = 900.0  # 15 min


def _issue(store, email="bob@acu.edu", site="acu-flowloop", roles=("researcher",), now=T0):
    return issue_magic_link(email, site, roles=roles, ttl_seconds=TTL, now=now, store=store)


# ------------------------------------------------------------------ issue -----
def test_issue_returns_a_prefixed_token_and_stores_only_a_hash():
    store = InMemoryMagicLinkStore()
    token = _issue(store)
    assert token.startswith("aml_")
    link_id, secret = split_magic_token(token)
    rec = store.get(link_id)
    assert rec is not None
    # "hashed at rest" as a check that CAN fail: the stored value is a real
    # scrypt hash (not the plaintext), it verifies THIS secret, and it rejects a
    # wrong one. If hashing were bypassed (plaintext stored) or wired to a
    # constant, one of these three flips.
    from axiom.webauth.password import verify_password
    assert rec.secret_hash.startswith("scrypt$")
    assert rec.secret_hash != secret
    assert verify_password(secret, rec.secret_hash) is True
    assert verify_password(secret + "tamper", rec.secret_hash) is False
    assert rec.email == "bob@acu.edu" and rec.site == "acu-flowloop"
    assert rec.roles == ("researcher",)
    assert rec.used_at is None


def test_issue_requires_a_site_bound_server_side():
    store = InMemoryMagicLinkStore()
    with pytest.raises(ValueError):
        issue_magic_link("bob@acu.edu", None, ttl_seconds=TTL, now=T0, store=store)


def test_issue_normalizes_email():
    store = InMemoryMagicLinkStore()
    token = _issue(store, email="  Bob@ACU.edu ")
    link_id, _ = split_magic_token(token)
    assert store.get(link_id).email == "bob@acu.edu"


# ----------------------------------------------------------------- redeem -----
def test_redeem_returns_the_bound_identity():
    store = InMemoryMagicLinkStore()
    token = _issue(store)
    identity = redeem_magic_link(token, now=T0 + 60, store=store)
    assert identity.email == "bob@acu.edu"
    assert identity.site == "acu-flowloop"      # site comes from issuance, never the redeemer
    assert identity.roles == ("researcher",)


def test_redeem_is_single_use():
    store = InMemoryMagicLinkStore()
    token = _issue(store)
    redeem_magic_link(token, now=T0 + 60, store=store)
    with pytest.raises(MagicLinkError):
        redeem_magic_link(token, now=T0 + 120, store=store)


def test_redeem_rejects_an_expired_link():
    store = InMemoryMagicLinkStore()
    token = _issue(store)
    with pytest.raises(MagicLinkError):
        redeem_magic_link(token, now=T0 + TTL + 1, store=store)


def test_redeem_rejects_an_unknown_link():
    store = InMemoryMagicLinkStore()
    with pytest.raises(MagicLinkError):
        redeem_magic_link("aml_deadbeef_nope", now=T0, store=store)


def test_redeem_rejects_a_tampered_secret():
    store = InMemoryMagicLinkStore()
    token = _issue(store)
    link_id, _ = split_magic_token(token)
    with pytest.raises(MagicLinkError):
        redeem_magic_link(f"aml_{link_id}_wrongsecret", now=T0 + 60, store=store)


def test_redeem_rejects_a_malformed_token():
    store = InMemoryMagicLinkStore()
    with pytest.raises(MagicLinkError):
        redeem_magic_link("not-a-magic-token", now=T0, store=store)


def test_split_magic_token_roundtrip_and_reject():
    assert split_magic_token("aml_abc_def") == ("abc", "def")
    assert split_magic_token("axk_abc_def") is None      # wrong prefix
    assert split_magic_token("aml_only") is None         # no secret


def test_expired_then_reissue_is_independent():
    """A fresh link after expiry works; the old one stays dead."""
    store = InMemoryMagicLinkStore()
    old = _issue(store, now=T0)
    new = _issue(store, now=T0 + TTL + 1)
    assert redeem_magic_link(new, now=T0 + TTL + 2, store=store).email == "bob@acu.edu"
    with pytest.raises(MagicLinkError):
        redeem_magic_link(old, now=T0 + TTL + 2, store=store)


def test_token_with_underscore_in_secret_redeems_regression():
    """Regression: link_id must be underscore-free so the token splits on the
    FIRST '_'; a secret that itself contains '_' must still redeem. (Caught a
    flaky bug where token_urlsafe put '_' in the id.)"""
    store = InMemoryMagicLinkStore()
    token = issue_magic_link(
        "bob@acu.edu", "acu-flowloop", ttl_seconds=TTL, now=T0, store=store,
        link_id="abc123def456", secret="has_underscores_in_it_xyz",
    )
    assert token == "aml_abc123def456_has_underscores_in_it_xyz"
    assert split_magic_token(token) == ("abc123def456", "has_underscores_in_it_xyz")
    assert redeem_magic_link(token, now=T0 + 1, store=store).email == "bob@acu.edu"
