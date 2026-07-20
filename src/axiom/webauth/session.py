# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Browser session tokens — the value carried in the session cookie.

A session is an ES256 JWT (``type=session``) minted from a :class:`User` at
login, verified from the public JWKS like every other Axiom token. Kept
framework-agnostic (no FastAPI import): the ``webgate`` extension owns the actual
``Set-Cookie``/read, this owns the crypto + the cookie *name*.

This is the seam the OIDC fast-follow reuses: ``oauth``'s ``SubjectResolver``
calls :func:`session_from_cookies` so an already-logged-in browser sails through
``/oauth/authorize`` — one login, cookie-session and OIDC alike.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta

from .jwt import create_access_token, verify_token
from .users import User

#: The session cookie name (also honoured by the OIDC resolver).
SESSION_COOKIE = "axiom_session"
#: Default browser session lifetime.
DEFAULT_SESSION_TTL = timedelta(hours=12)

_SESSION_TYPE = "session"


def issue_session_token(
    user: User,
    *,
    ttl: timedelta | None = None,
    issuer: str | None = None,
) -> str:
    """Mint a session token for ``user`` (``type=session``, ES256)."""
    claims = {
        "sub": user.user_id,
        "email": user.email,
        "name": user.name,
        "roles": list(user.roles),
        "type": _SESSION_TYPE,
    }
    return create_access_token(
        claims, expires_delta=ttl or DEFAULT_SESSION_TTL, issuer=issuer
    )


def verify_session_token(token: str, *, issuer: str | None = None) -> dict | None:
    """Verify a session token. Returns claims, or ``None`` on any failure.

    A token that is not ``type=session`` (e.g. a plain access token) is refused,
    so an API token can never be replayed as a browser session.
    """
    claims = verify_token(token, issuer=issuer)
    if claims is None or claims.get("type") != _SESSION_TYPE:
        return None
    return claims


def session_from_cookies(
    cookies: Mapping[str, str], *, issuer: str | None = None
) -> dict | None:
    """Read + verify the session from a cookie mapping (``request.cookies``)."""
    token = cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return verify_session_token(token, issuer=issuer)


__all__ = [
    "DEFAULT_SESSION_TTL",
    "SESSION_COOKIE",
    "issue_session_token",
    "session_from_cookies",
    "verify_session_token",
]
