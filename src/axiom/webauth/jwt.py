# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
#
# Ported from SoilMetrix, Inc (dba Aiterra.ag) by Benjamin Booth, CEO.

"""JWT token creation and verification.

The default signing path is **ES256** (ADR-085): tokens are signed with the
node's private EC key and verified from the public JWKS, so a resource server or
MCP client never needs a shared secret to validate them. Access tokens are typed
``at+jwt`` (RFC 9068) and carry the signing key's ``kid`` so verifiers select the
right JWK across rotations. ``audience`` (RFC 8707) and ``issuer`` bind a token
to where it may be presented.

HS256 remains reachable only by passing an explicit ``secret_key`` — the
migration-window compatibility path for tokens minted before the ES256 cutover.
It is not the default and will be removed once no HS256 tokens remain in flight.

Lift note: uses PyJWT (Axiom's existing JWT dependency) in place of python-jose,
and timezone-aware expiries in place of the deprecated ``datetime.utcnow()``.
``verify_token`` returns the decoded payload, or ``None`` on any failure (bad
signature, expired, wrong audience/issuer, unknown ``kid``, malformed).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt

from .config import ACCESS_TOKEN_EXPIRE_MINUTES, REFRESH_TOKEN_EXPIRE_DAYS
from .keys import SigningKey, get_key_store

_HS_DEFAULT = "HS256"
#: RFC 9068 media type for OAuth 2.0 JWT access tokens.
_ACCESS_TYP = "at+jwt"


def _encode(
    claims: dict,
    *,
    key: SigningKey | None,
    secret_key: str | None,
    algorithm: str | None,
    typ: str | None,
) -> str:
    if secret_key is not None:
        # Legacy HS256 compat path (symmetric secret, no kid). Migration window.
        return jwt.encode(claims, secret_key, algorithm=algorithm or _HS_DEFAULT)
    signing = key or get_key_store().active
    headers: dict = {"kid": signing.kid}
    if typ is not None:
        headers["typ"] = typ
    return jwt.encode(claims, signing.signing_key, algorithm=signing.alg, headers=headers)


def create_access_token(
    data: dict,
    expires_delta: timedelta | None = None,
    *,
    key: SigningKey | None = None,
    secret_key: str | None = None,
    algorithm: str | None = None,
    audience: str | None = None,
    issuer: str | None = None,
) -> str:
    """Create a JWT access token (``type=access``, ``typ=at+jwt``).

    ``data`` must include ``sub`` (the principal handle). ``expires_delta``
    overrides the default lifetime. ``audience``/``issuer`` bind the token
    (RFC 8707). ``key`` signs with a specific ``SigningKey``; ``secret_key``
    selects the legacy HS256 path (tests / migration).
    """
    claims = data.copy()
    claims.setdefault("type", "access")
    claims["exp"] = datetime.now(UTC) + (
        expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    if audience is not None:
        claims["aud"] = audience
    if issuer is not None:
        claims["iss"] = issuer
    return _encode(
        claims, key=key, secret_key=secret_key, algorithm=algorithm, typ=_ACCESS_TYP
    )


def create_refresh_token(
    data: dict,
    *,
    key: SigningKey | None = None,
    secret_key: str | None = None,
    algorithm: str | None = None,
    audience: str | None = None,
    issuer: str | None = None,
) -> str:
    """Create a JWT refresh token (``type=refresh``).

    Note: ADR-085 targets opaque, rotating refresh tokens with reuse detection;
    this remains a JWT refresh token for the compat window until that store lands.
    """
    claims = data.copy()
    claims["type"] = "refresh"
    claims["exp"] = datetime.now(UTC) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
    if audience is not None:
        claims["aud"] = audience
    if issuer is not None:
        claims["iss"] = issuer
    return _encode(claims, key=key, secret_key=secret_key, algorithm=algorithm, typ=None)


def verify_token(
    token: str,
    *,
    key: SigningKey | None = None,
    secret_key: str | None = None,
    algorithm: str | None = None,
    audience: str | None = None,
    issuer: str | None = None,
) -> dict | None:
    """Verify and decode a JWT. Returns the payload, or ``None`` on failure.

    ES256 (default): the token's ``kid`` selects the verifying JWK from the key
    store (or ``key`` if given). ``audience`` is enforced only when supplied, so
    a resource server always passes its own; ``issuer`` likewise. HS256 (compat):
    pass ``secret_key``.
    """
    options = {"verify_aud": audience is not None}
    decode_kwargs: dict = {}
    if audience is not None:
        decode_kwargs["audience"] = audience
    if issuer is not None:
        decode_kwargs["issuer"] = issuer

    try:
        if secret_key is not None:
            return jwt.decode(
                token,
                secret_key,
                algorithms=[algorithm or _HS_DEFAULT],
                options=options,
                **decode_kwargs,
            )
        header = jwt.get_unverified_header(token)
        signing = key or get_key_store().get(header.get("kid"))
        if signing is None:
            return None
        return jwt.decode(
            token,
            signing.verifying_key,
            algorithms=[signing.alg],
            options=options,
            **decode_kwargs,
        )
    except jwt.PyJWTError:
        return None
