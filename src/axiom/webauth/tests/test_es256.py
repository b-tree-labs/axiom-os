# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
#
# Ported from SoilMetrix, Inc (dba Aiterra.ag) by Benjamin Booth, CEO.

"""ES256 is the default webauth signing path; HS256 stays verify/mint compat.

Covers ADR-085: asymmetric session tokens (kid header, at+jwt typ, JWKS
verification, audience/issuer binding) with HS256 preserved only as an explicit
legacy path for the migration window.
"""

from __future__ import annotations

import jwt as pyjwt

from axiom.webauth import create_access_token, create_refresh_token, verify_token
from axiom.webauth.keys import get_key_store, reset_key_store_for_tests

HS_SECRET = "k" * 40  # >= 32 bytes: avoids PyJWT's short-key warning


def setup_function() -> None:
    reset_key_store_for_tests()


def test_default_access_token_is_es256_with_kid_and_typ() -> None:
    tok = create_access_token({"sub": "@u:webapp"})
    header = pyjwt.get_unverified_header(tok)
    assert header["alg"] == "ES256"
    assert header["typ"] == "at+jwt"  # RFC 9068
    assert header["kid"] == get_key_store().active.kid

    payload = verify_token(tok)
    assert payload is not None
    assert payload["sub"] == "@u:webapp"
    assert payload["type"] == "access"


def test_default_verify_selects_key_by_kid_from_jwks() -> None:
    tok = create_access_token({"sub": "@u:webapp"})
    # A verifier with only the public JWKS (no shared secret) can validate it.
    store = get_key_store()
    kid = pyjwt.get_unverified_header(tok)["kid"]
    jwk = next(k for k in store.jwks()["keys"] if k["kid"] == kid)
    pub = pyjwt.algorithms.ECAlgorithm.from_jwk(jwk)
    decoded = pyjwt.decode(tok, pub, algorithms=["ES256"])
    assert decoded["sub"] == "@u:webapp"


def test_audience_binding_enforced_when_expected() -> None:
    tok = create_access_token({"sub": "@u:webapp"}, audience="https://api.example")
    assert verify_token(tok, audience="https://api.example") is not None
    assert verify_token(tok, audience="https://other.example") is None
    # No expected audience → aud not enforced (RS always passes its own).
    assert verify_token(tok) is not None


def test_issuer_binding_enforced_when_expected() -> None:
    tok = create_access_token({"sub": "@u:webapp"}, issuer="https://node.example")
    assert verify_token(tok, issuer="https://node.example") is not None
    assert verify_token(tok, issuer="https://evil.example") is None


def test_refresh_token_marked_and_es256() -> None:
    tok = create_refresh_token({"sub": "@u:webapp"})
    assert pyjwt.get_unverified_header(tok)["alg"] == "ES256"
    assert verify_token(tok)["type"] == "refresh"


def test_es256_token_not_verifiable_with_hs_secret() -> None:
    tok = create_access_token({"sub": "@u:webapp"})  # ES256
    assert verify_token(tok, secret_key=HS_SECRET) is None


def test_hs256_compat_mint_and_verify_still_work() -> None:
    # Legacy path: explicit secret_key keeps HS256 mint+verify during migration.
    tok = create_access_token({"sub": "@u:webapp"}, secret_key=HS_SECRET)
    assert pyjwt.get_unverified_header(tok)["alg"] == "HS256"
    assert verify_token(tok, secret_key=HS_SECRET) is not None
    # An HS256 token is not accepted by the default ES256 verify path.
    assert verify_token(tok) is None
