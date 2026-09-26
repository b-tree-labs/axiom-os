# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Verifying a password must not depend on when the hash was made.

This module hashed with stdlib scrypt and verified scrypt only, which was
correct while the only consumer was greenfield. Its own lift note recorded the
consequence and the remedy: an existing product stores bcrypt hashes, and until
Axiom can verify those, adopting Axiom's auth would fail every login made before
the switch.

That is the whole cost of a platform that only supports what it happens to have
written. So verification now dispatches on the scheme recorded in the stored
hash, and a consumer arriving with history can adopt the platform without
invalidating it.

Two properties matter beyond "it verifies":

*Upgrade on verify.* A legacy hash that checks out is reported as needing
rehash, so a caller can re-store it in the current scheme at the one moment the
plaintext is legitimately in hand. Without that, a migration is either a
flag-day or never finishes.

*Loud when it cannot.* bcrypt is an optional dependency. If a legacy hash
arrives and the library is absent, that is a deployment error, and it has to say
so — a silent False is indistinguishable from a wrong password, which would send
an operator hunting a user's typing while every historical login fails.
"""

from __future__ import annotations

import logging

import pytest

from axiom.webauth import password as pw

# A real bcrypt digest of "correct horse battery staple", $2b$ with cost 4.
# Committed as a constant so the routing is testable wherever bcrypt is absent.
_BCRYPT_HASH = "$2b$04$eG7VJ0kGZ8n3wJ1zqzO0IeKZ1xq7oQ1Q0mQ1x1Q1x1Q1x1Q1x1Q1x"
_BCRYPT_PLAIN = "correct horse battery staple"

_have_bcrypt = pw.bcrypt_available()


def test_a_current_hash_still_verifies():
    """The scheme that already worked must keep working."""
    h = pw.get_password_hash("hunter2")
    assert pw.verify_password("hunter2", h)
    assert not pw.verify_password("hunter3", h)


def test_a_current_hash_does_not_ask_to_be_rehashed():
    assert not pw.needs_rehash(pw.get_password_hash("hunter2"))


def test_a_legacy_hash_is_recognised_as_legacy():
    """Recognition must not require the library — routing is separable."""
    assert pw.scheme_of(_BCRYPT_HASH) == "bcrypt"
    assert pw.scheme_of(pw.get_password_hash("x")) == "scrypt"


def test_a_legacy_hash_asks_to_be_rehashed():
    """This is the migration: upgrade at the one moment the plaintext is here."""
    assert pw.needs_rehash(_BCRYPT_HASH)


def test_an_unknown_scheme_is_refused_not_guessed():
    assert pw.scheme_of("not-a-hash") is None
    assert not pw.verify_password("anything", "not-a-hash")


@pytest.mark.skipif(_have_bcrypt, reason="bcrypt installed; this covers its absence")
def test_a_legacy_hash_without_the_library_is_loud_not_silent(caplog):
    """A missing optional dependency must not look like a wrong password.

    Silent False here would send an operator hunting a user's typing while
    every pre-migration login failed for an unrelated reason.
    """
    with caplog.at_level(logging.ERROR, logger="axiom.webauth.password"):
        assert pw.verify_password(_BCRYPT_PLAIN, _BCRYPT_HASH) is False
    blob = " ".join(r.message for r in caplog.records).lower()
    assert "bcrypt" in blob and "install" in blob, (
        f"the failure was not explained: {[r.message for r in caplog.records]}"
    )


@pytest.mark.skipif(not _have_bcrypt, reason="bcrypt not installed here")
def test_a_legacy_hash_verifies_when_the_library_is_present():
    """The real path. Only meaningful where the optional dep is installed."""
    import bcrypt as _b

    made = _b.hashpw(b"s3cret", _b.gensalt(rounds=4)).decode()
    assert pw.verify_password("s3cret", made)
    assert not pw.verify_password("wrong", made)
    assert pw.needs_rehash(made)


def test_the_module_plans_around_no_particular_consumer():
    """Domain-agnostic: legacy bcrypt support is a platform capability.

    This checks for *coupling*, not for the attribution line. A credit naming
    where code was ported from is provenance and belongs in the header; the
    platform's own mirror guard forbids a consumer's identifiers, not a credit.
    What must not survive is Axiom planning around one consumer's schema — this
    module previously carried a TODO to add bcrypt support "if/when we unify
    <a named product>'s user table", which made a general capability look like
    one customer's favour.
    """
    import pathlib

    text = pathlib.Path(pw.__file__).read_text(encoding="utf-8")
    body = "\n".join(
        line for line in text.splitlines()
        if not line.startswith("# Ported from")
    ).lower()
    for word in ("soilmetrix", "aiterra", "agbench", "neutron"):
        assert word not in body, f"{word!r} couples platform auth to a consumer"
