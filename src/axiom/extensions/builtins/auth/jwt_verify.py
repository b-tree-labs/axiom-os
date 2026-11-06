# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""id_token verification against the IdP JWKS (AUTH-5, AUTH-R7).

Verifies the RS256 signature against the matching JWK, then validates
``iss``/``aud``/``exp``/``nbf`` (with leeway). Only after this is the principal
trustworthy for ``authz``. No JWT library — just ``cryptography``.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class TokenVerificationError(Exception):
    """The id_token failed signature or claim validation."""


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _rsa_public_key(jwk: dict):
    n = int.from_bytes(_b64url_decode(jwk["n"]), "big")
    e = int.from_bytes(_b64url_decode(jwk["e"]), "big")
    return rsa.RSAPublicNumbers(e, n).public_key()


def _find_jwk(jwks: dict, kid: Optional[str]) -> Optional[dict]:
    for key in jwks.get("keys", []):
        if kid is None or key.get("kid") == kid:
            return key
    return None


def verify_id_token(
    id_token: str,
    *,
    jwks: dict,
    issuer: str,
    audience: str,
    now: Optional[float] = None,
    leeway: int = 60,
) -> dict:
    """Return the verified claims, or raise ``TokenVerificationError``."""
    now = now if now is not None else time.time()
    try:
        header_b64, payload_b64, sig_b64 = id_token.split(".")
    except ValueError as exc:
        raise TokenVerificationError("malformed id_token") from exc

    header = json.loads(_b64url_decode(header_b64))
    payload = json.loads(_b64url_decode(payload_b64))
    if header.get("alg") != "RS256":
        raise TokenVerificationError(f"unsupported alg {header.get('alg')!r}")

    jwk = _find_jwk(jwks, header.get("kid"))
    if jwk is None:
        raise TokenVerificationError(f"no JWK for kid {header.get('kid')!r}")

    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    try:
        _rsa_public_key(jwk).verify(
            _b64url_decode(sig_b64), signing_input, padding.PKCS1v15(), hashes.SHA256()
        )
    except InvalidSignature as exc:
        raise TokenVerificationError("id_token signature is invalid") from exc

    if payload.get("iss") != issuer:
        raise TokenVerificationError("issuer mismatch")
    aud = payload.get("aud")
    if audience not in (aud if isinstance(aud, list) else [aud]):
        raise TokenVerificationError("audience mismatch")
    if "exp" in payload and now > payload["exp"] + leeway:
        raise TokenVerificationError("id_token expired")
    if "nbf" in payload and now < payload["nbf"] - leeway:
        raise TokenVerificationError("id_token not yet valid")
    return payload


__all__ = ["TokenVerificationError", "verify_id_token"]
