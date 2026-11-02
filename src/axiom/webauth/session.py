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

import os
import time
from collections.abc import Mapping
from datetime import timedelta

from .jwt import create_access_token, verify_token
from .users import User

#: The session cookie name (also honoured by the OIDC resolver).
SESSION_COOKIE = "axiom_session"
#: Default browser session lifetime.
DEFAULT_SESSION_TTL = timedelta(hours=12)

_SESSION_TYPE = "session"

#: Per-process session realm (VAR-A2). ``axi app run`` sets these for each
#: variant it supervises; a plain install sets none and keeps today's cookie
#: and realm-less tokens. Read at call time so every call site honours them.
REALM_ENV = "AXIOM_SESSION_REALM"
COOKIE_ENV = "AXIOM_SESSION_COOKIE"
LEGACY_ENV = "AXIOM_SESSION_ACCEPT_LEGACY"

_UNSET: object = object()


def session_realm() -> str | None:
    """The realm this process mints and accepts sessions in; ``None`` for a plain install."""
    return (os.environ.get(REALM_ENV) or "").strip() or None


def session_cookie_name() -> str:
    """The cookie this process reads and writes the session in."""
    return (os.environ.get(COOKIE_ENV) or "").strip() or SESSION_COOKIE


def accepts_legacy_sessions() -> bool:
    """Whether a realm-less session from the plain install is still honoured here.

    Only the variant that takes the install over in place says yes, so the
    people already signed in are not made to sign in again.
    """
    return (os.environ.get(LEGACY_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def issue_session_token(
    user: User,
    *,
    ttl: timedelta | None = None,
    issuer: str | None = None,
    amr: tuple[str, ...] | None = None,
    auth_time: int | None = None,
    realm: str | None | object = _UNSET,
) -> str:
    """Mint a session token for ``user`` (``type=session``, ES256).

    ``realm`` (default: this process's, :func:`session_realm`) is signed into
    the token, so a session minted in one variant's realm is refused by every
    other (VAR-A2).

    Every call site is a sign-in, so ``auth_time`` defaults to now; an OIDC
    sign-in passes the IdP's own ``auth_time``, because a silent SSO reuses an
    older IdP session and must not look fresh. ``amr`` names how
    the person authenticated (RFC 8176 values: ``pwd``, ``otp``, ``mfa``, …);
    when the caller does not say, it is left out rather than guessed. A
    signing grant reads both to enforce a book's ``fresh_within`` (ADR-146).
    """
    claims = {
        "sub": user.user_id,
        "email": user.email,
        "name": user.name,
        "roles": list(user.roles),
        "type": _SESSION_TYPE,
        "auth_time": int(auth_time if auth_time is not None else time.time()),
    }
    if amr:
        claims["amr"] = list(amr)
    # An IdP-backed account (OIDC sign-in / linked) carries its provider so the
    # authz edge can grade the session ``sso`` rather than ``attested``.
    idp = (user.attributes or {}).get("idp") if isinstance(user.attributes, dict) else None
    if idp:
        claims["idp"] = str(idp)
    if user.site:
        claims["site"] = user.site
    the_realm = session_realm() if realm is _UNSET else realm
    if the_realm:
        claims["realm"] = str(the_realm)
    return create_access_token(
        claims, expires_delta=ttl or DEFAULT_SESSION_TTL, issuer=issuer
    )


def verify_session_token(
    token: str,
    *,
    issuer: str | None = None,
    realm: str | None | object = _UNSET,
    accept_legacy: bool | None = None,
) -> dict | None:
    """Verify a session token. Returns claims, or ``None`` on any failure.

    A token that is not ``type=session`` (e.g. a plain access token) is refused,
    so an API token can never be replayed as a browser session.

    The token's realm must be this process's (``realm``, default
    :func:`session_realm`): a token from another realm is refused, and so is a
    realm-bearing token on a plain install. A realm-less token is honoured in a
    realm only where ``accept_legacy`` (default :func:`accepts_legacy_sessions`).
    """
    claims = verify_token(token, issuer=issuer)
    if claims is None or claims.get("type") != _SESSION_TYPE:
        return None
    expected = session_realm() if realm is _UNSET else realm
    got = claims.get("realm") or None
    if got == (expected or None):
        return claims
    legacy = accepts_legacy_sessions() if accept_legacy is None else accept_legacy
    if got is None and legacy:
        return claims
    return None


def session_from_cookies(
    cookies: Mapping[str, str], *, issuer: str | None = None
) -> dict | None:
    """Read + verify the session from a cookie mapping (``request.cookies``).

    Reads this process's cookie (:func:`session_cookie_name`); where legacy
    sessions are accepted, the plain install's cookie is read after it.
    """
    name = session_cookie_name()
    token = cookies.get(name)
    if not token and name != SESSION_COOKIE and accepts_legacy_sessions():
        token = cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return verify_session_token(token, issuer=issuer)


__all__ = [
    "DEFAULT_SESSION_TTL",
    "SESSION_COOKIE",
    "accepts_legacy_sessions",
    "issue_session_token",
    "session_cookie_name",
    "session_realm",
    "session_from_cookies",
    "verify_session_token",
]
