# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""AUTH-5/AUTH-R7: id_token JWKS signature + claim verification, with a real
self-generated RSA key (no live IdP)."""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from axiom.extensions.builtins.auth.jwt_verify import (
    TokenVerificationError,
    verify_id_token,
)

ISS = "https://login.microsoftonline.com/example-tenant/v2.0"
AUD = "client-123"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _jwks(priv, kid="k1"):
    nums = priv.public_key().public_numbers()
    return {"keys": [{
        "kty": "RSA", "kid": kid,
        "n": _b64(nums.n.to_bytes((nums.n.bit_length() + 7) // 8, "big")),
        "e": _b64(nums.e.to_bytes((nums.e.bit_length() + 7) // 8, "big")),
    }]}


def _jwt(priv, claims, kid="k1"):
    header = _b64(json.dumps({"alg": "RS256", "kid": kid, "typ": "JWT"}).encode())
    payload = _b64(json.dumps(claims).encode())
    sig = priv.sign(f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64(sig)}"


def _claims(**over):
    c = {"iss": ISS, "aud": AUD, "exp": 2000, "nbf": 500, "sub": "eid-7", "email": "user@example.org"}
    c.update(over)
    return c


def test_valid_token_verifies_and_returns_claims():
    priv = _key()
    claims = verify_id_token(_jwt(priv, _claims()), jwks=_jwks(priv),
                             issuer=ISS, audience=AUD, now=1000)
    assert claims["sub"] == "eid-7" and claims["email"] == "user@example.org"


def test_wrong_signing_key_is_rejected():
    priv, attacker = _key(), _key()
    token = _jwt(attacker, _claims())                       # signed by a different key
    with pytest.raises(TokenVerificationError, match="signature"):
        verify_id_token(token, jwks=_jwks(priv), issuer=ISS, audience=AUD, now=1000)


def test_expired_and_wrong_audience_and_issuer_rejected():
    priv = _key()
    with pytest.raises(TokenVerificationError, match="expired"):
        verify_id_token(_jwt(priv, _claims()), jwks=_jwks(priv), issuer=ISS, audience=AUD, now=5000)
    with pytest.raises(TokenVerificationError, match="audience"):
        verify_id_token(_jwt(priv, _claims(aud="other")), jwks=_jwks(priv), issuer=ISS, audience=AUD, now=1000)
    with pytest.raises(TokenVerificationError, match="issuer"):
        verify_id_token(_jwt(priv, _claims(iss="https://evil")), jwks=_jwks(priv), issuer=ISS, audience=AUD, now=1000)


def test_unknown_kid_rejected():
    priv = _key()
    with pytest.raises(TokenVerificationError, match="JWK"):
        verify_id_token(_jwt(priv, _claims(), kid="other"), jwks=_jwks(priv, kid="k1"),
                        issuer=ISS, audience=AUD, now=1000)
