# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
#
# Ported from SoilMetrix, Inc (dba Aiterra.ag) by Benjamin Booth, CEO.

"""Tests for webauth ES256 signing keys + JWKS (ADR-085)."""

from __future__ import annotations

import base64
import hashlib
import json

import jwt as pyjwt

from axiom.webauth.keys import KeyStore, SigningKey


def test_generate_is_es256_p256() -> None:
    sk = SigningKey.generate()
    assert sk.alg == "ES256"
    jwk = sk.public_jwk()
    assert jwk["kty"] == "EC"
    assert jwk["crv"] == "P-256"
    assert jwk["use"] == "sig"
    assert jwk["alg"] == "ES256"
    # Public JWK must never carry the private scalar.
    assert "d" not in jwk


def test_kid_is_rfc7638_thumbprint_and_stable() -> None:
    sk = SigningKey.generate()
    jwk = sk.public_jwk()
    # RFC 7638: SHA-256 of the canonical JWK (required EC members crv/kty/x/y,
    # lexicographic, no whitespace), base64url without padding.
    canonical = json.dumps(
        {"crv": "P-256", "kty": "EC", "x": jwk["x"], "y": jwk["y"]},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    expected = base64.urlsafe_b64encode(hashlib.sha256(canonical).digest()).rstrip(b"=")
    assert sk.kid == expected.decode()
    assert jwk["kid"] == sk.kid


def test_pem_roundtrip_preserves_identity() -> None:
    sk = SigningKey.generate()
    pem = sk.to_pem()
    assert "PRIVATE KEY" in pem
    restored = SigningKey.from_pem(pem)
    assert restored.kid == sk.kid
    assert restored.public_jwk() == sk.public_jwk()


def test_sign_and_verify_via_public_key() -> None:
    sk = SigningKey.generate()
    token = pyjwt.encode({"sub": "@u:webapp"}, sk.signing_key, algorithm="ES256")
    decoded = pyjwt.decode(token, sk.verifying_key, algorithms=["ES256"])
    assert decoded["sub"] == "@u:webapp"


def test_keystore_active_get_and_jwks() -> None:
    active = SigningKey.generate()
    retired = SigningKey.generate()
    store = KeyStore([active, retired], active_kid=active.kid)

    assert store.active.kid == active.kid
    assert store.get(active.kid) is active
    assert store.get(retired.kid) is retired
    assert store.get("nope") is None

    jwks = store.jwks()
    kids = {k["kid"] for k in jwks["keys"]}
    assert kids == {active.kid, retired.kid}
    # JWKS is public-only.
    assert all("d" not in k for k in jwks["keys"])
