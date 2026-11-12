# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Grant handlers for the oauth AS (ADR-082).

Pure token-issuance logic, independent of HTTP: given an authenticated client and
the request parameters, return the RFC 6749 §5.1 token response or raise an
:class:`OAuthError`. The router (:mod:`.api.routers`) owns transport + client
authentication; this owns the grant rules. This cut ships ``client_credentials``.
"""

from __future__ import annotations

from datetime import timedelta

from axiom.webauth import create_access_token
from axiom.webauth.config import ACCESS_TOKEN_EXPIRE_MINUTES

from .codes import AuthorizationCode
from .errors import (
    INVALID_SCOPE,
    INVALID_TARGET,
    UNAUTHORIZED_CLIENT,
    OAuthError,
)
from .models import OAuthClient
from .refresh import RefreshToken, RefreshTokenStore

GRANT_CLIENT_CREDENTIALS = "client_credentials"
GRANT_AUTHORIZATION_CODE = "authorization_code"
GRANT_REFRESH_TOKEN = "refresh_token"
#: Scope that requests a refresh token (offline, no-user-present access) — OIDC.
OFFLINE_ACCESS = "offline_access"


def _attach_refresh(
    response: dict,
    *,
    store: RefreshTokenStore | None,
    client_id: str,
    subject: str,
    scope: str,
    resource: str | None,
    family_id: str | None = None,
) -> dict:
    """Add a rotating refresh token to ``response`` when offline access applies.

    Only when a store is configured and ``offline_access`` was granted — a
    client that did not ask for offline access never receives a refresh token.
    """
    if store is not None and OFFLINE_ACCESS in scope.split():
        issued = store.issue(
            client_id=client_id, subject=subject, scope=scope,
            resource=resource, family_id=family_id,
        )
        response["refresh_token"] = issued.token
    return response


def _token_response(
    *, subject: str, client_id: str, scope: str, audience: str, issuer: str,
    ttl: timedelta,
) -> dict:
    """Mint an access token and wrap it as an RFC 6749 §5.1 response."""
    claims: dict = {"sub": subject, "client_id": client_id}
    if scope:
        claims["scope"] = scope
    token = create_access_token(claims, expires_delta=ttl, audience=audience, issuer=issuer)
    response: dict = {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": int(ttl.total_seconds()),
    }
    if scope:
        response["scope"] = scope
    return response


def resolve_scope(requested: str | None, allowed: tuple[str, ...]) -> str:
    """Narrow the requested scope within the client's ceiling.

    Omitted → the AS grants the client's full registered scope set (RFC 6749
    §3.3). Present → every requested scope must be permitted, else invalid_scope.
    Scope is space-delimited (RFC 6749 §3.3). Shared by ``/authorize`` and the
    ``client_credentials`` grant.
    """
    allowed_set = set(allowed)
    if requested is None:
        return " ".join(allowed)
    requested_list = requested.split()
    disallowed = [s for s in requested_list if s not in allowed_set]
    if disallowed:
        raise OAuthError(
            INVALID_SCOPE, f"scope(s) not permitted for client: {' '.join(disallowed)}"
        )
    return " ".join(requested_list)


def validate_resource(resource: str | None, client: OAuthClient) -> str | None:
    """Check a requested resource indicator (RFC 8707) against the client.

    Present → must be one of the client's registered audiences, else
    invalid_target. Omitted → ``None`` (the caller decides the default).
    """
    if resource is not None and resource not in client.audiences:
        raise OAuthError(INVALID_TARGET, f"resource not permitted: {resource}")
    return resource


def resolve_audience(resource: str | None, client: OAuthClient, issuer: str) -> str:
    """Token audience: the validated resource, or the issuer as the safe default.

    Never mints an unaudienced bearer token — an omitted resource binds the token
    to the AS itself.
    """
    return validate_resource(resource, client) or issuer


def issue_client_credentials(
    *,
    client: OAuthClient,
    requested_scope: str | None,
    resource: str | None,
    issuer: str,
    lifetime: timedelta | None = None,
) -> dict:
    """Issue an access token for the ``client_credentials`` grant (RFC 6749 §4.4).

    The subject is the client itself (no end user). Returns the token response
    dict; raises :class:`OAuthError` on any policy failure.
    """
    if GRANT_CLIENT_CREDENTIALS not in client.grant_types:
        raise OAuthError(
            UNAUTHORIZED_CLIENT,
            "client is not permitted to use the client_credentials grant",
        )

    granted_scope = resolve_scope(requested_scope, client.scopes)
    audience = resolve_audience(resource, client, issuer)
    ttl = lifetime or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    # The subject of a client_credentials token is the client itself (no end user).
    return _token_response(
        subject=client.client_id,
        client_id=client.client_id,
        scope=granted_scope,
        audience=audience,
        issuer=issuer,
        ttl=ttl,
    )


def issue_authorization_code_token(
    *,
    record: AuthorizationCode,
    issuer: str,
    refresh_store: RefreshTokenStore | None = None,
    lifetime: timedelta | None = None,
) -> dict:
    """Issue an access token for a redeemed authorization code (RFC 6749 §4.1.3).

    The code was validated (client + redirect_uri binding, PKCE) by the caller;
    here we mint the token for the resource owner the code was issued to, plus a
    refresh token when ``offline_access`` was granted. The audience is the code's
    bound resource, or the issuer when none was requested (never unaudienced).
    """
    ttl = lifetime or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    response = _token_response(
        subject=record.subject,
        client_id=record.client_id,
        scope=record.scope,
        audience=record.resource or issuer,
        issuer=issuer,
        ttl=ttl,
    )
    return _attach_refresh(
        response, store=refresh_store, client_id=record.client_id,
        subject=record.subject, scope=record.scope, resource=record.resource,
    )


def issue_refresh_token_grant(
    *,
    record: RefreshToken,
    requested_scope: str | None,
    issuer: str,
    refresh_store: RefreshTokenStore,
    lifetime: timedelta | None = None,
) -> dict:
    """Mint a token from a rotated refresh token (RFC 6749 §6).

    ``record`` is the token the store just rotated (consumed). Scope may narrow
    within the original grant but never widen (invalid_scope). A fresh refresh
    token is issued into the same family so the rotation chain — and reuse
    detection — continues.
    """
    granted_scope = resolve_scope(requested_scope, tuple(record.scope.split()))
    ttl = lifetime or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    response = _token_response(
        subject=record.subject,
        client_id=record.client_id,
        scope=granted_scope,
        audience=record.resource or issuer,
        issuer=issuer,
        ttl=ttl,
    )
    # Always continue the rotation chain on a refresh, in the same family.
    issued = refresh_store.issue(
        client_id=record.client_id, subject=record.subject, scope=granted_scope,
        resource=record.resource, family_id=record.family_id,
    )
    response["refresh_token"] = issued.token
    return response


__all__ = [
    "GRANT_AUTHORIZATION_CODE",
    "GRANT_CLIENT_CREDENTIALS",
    "GRANT_REFRESH_TOKEN",
    "OFFLINE_ACCESS",
    "issue_authorization_code_token",
    "issue_client_credentials",
    "issue_refresh_token_grant",
    "resolve_audience",
    "resolve_scope",
    "validate_resource",
]
