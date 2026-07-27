# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The oauth AS HTTP surface (ADR-082).

Two public routers, both mounted with ``requires_authz=False`` — an OAuth client
must reach these before it holds any token, and the token endpoint authenticates
the *client* from the request itself (Basic / private_key_jwt), not via GUARD:

- :func:`build_oauth_router` — discovery + JWKS at ``/.well-known/*``, the front
  door every OAuth 2.1 / OIDC / MCP client fetches first.
- :func:`build_oauth_endpoints_router` — the token endpoint at ``/oauth/token``.
  This cut ships the ``client_credentials`` grant; ``/oauth/authorize`` and the
  authorization-code exchange land in the next cut (URLs already advertised).
"""

from __future__ import annotations

import base64
import binascii
import os
from collections.abc import Callable
from urllib.parse import unquote, urlencode, urlparse

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from axiom.webauth import get_key_store, verify_password

from ..clients import ClientRegistry, get_client_registry
from ..codes import AuthorizationCodeStore, get_authorization_code_store
from ..errors import (
    INVALID_GRANT,
    INVALID_REQUEST,
    UNAUTHORIZED_CLIENT,
    UNSUPPORTED_GRANT_TYPE,
    UNSUPPORTED_RESPONSE_TYPE,
    OAuthError,
    invalid_client,
)
from ..grants import (
    GRANT_AUTHORIZATION_CODE,
    GRANT_CLIENT_CREDENTIALS,
    GRANT_REFRESH_TOKEN,
    issue_authorization_code_token,
    issue_client_credentials,
    issue_refresh_token_grant,
    resolve_scope,
    validate_resource,
)
from ..metadata import authorization_server_metadata, openid_configuration
from ..models import OAuthClient
from ..pkce import S256, verify_pkce
from ..refresh import RefreshTokenStore, get_refresh_token_store

#: Resolves the authenticated resource owner from the request (session cookie /
#: bearer), or ``None`` when nobody is logged in. The default knows no session
#: mechanism — a real deployment (the webapp) injects one; unresolved means the
#: authorize endpoint redirects to the login page.
SubjectResolver = Callable[[Request], "str | None"]

#: Where ``/oauth/authorize`` sends an unauthenticated user. The webapp serves
#: the login page and redirects back to ``return_to`` after authentication.
DEFAULT_LOGIN_PATH = "/login"

_SUBJECT_RESOLVER: SubjectResolver | None = None


def get_subject_resolver() -> SubjectResolver:
    """The process-wide resource-owner resolver (default: nobody logged in)."""
    return _SUBJECT_RESOLVER or (lambda _request: None)


def set_subject_resolver(resolver: SubjectResolver) -> None:
    """Wire the resource-owner resolver — the webapp installs one reading its
    session so the composed ``/oauth/authorize`` can authenticate users."""
    global _SUBJECT_RESOLVER
    _SUBJECT_RESOLVER = resolver


def reset_subject_resolver_for_tests() -> None:
    global _SUBJECT_RESOLVER
    _SUBJECT_RESOLVER = None


def _issuer(request: Request) -> str:
    """The AS issuer URL.

    Prefers ``OAUTH_ISSUER`` (the public URL, e.g. behind a TLS-terminating
    reverse proxy where the request's own host is the internal one); otherwise
    derives from the incoming request so it is correct on any host with no config.
    """
    override = os.getenv("OAUTH_ISSUER")
    if override:
        return override.rstrip("/")
    return str(request.base_url).rstrip("/")


def build_oauth_router() -> APIRouter:
    """Assemble the public discovery + JWKS router mounted at ``/.well-known``."""
    router = APIRouter(tags=["oauth"])

    @router.get("/.well-known/jwks.json")
    def jwks() -> dict:
        """RFC 7517 JWK Set — the public ES256 keys clients verify tokens with."""
        return get_key_store().jwks()

    @router.get("/.well-known/oauth-authorization-server")
    def authorization_server(request: Request) -> dict:
        return authorization_server_metadata(_issuer(request))

    @router.get("/.well-known/openid-configuration")
    def openid(request: Request) -> dict:
        return openid_configuration(_issuer(request))

    return router


def _authenticate_client_basic(
    request: Request, registry: ClientRegistry
) -> OAuthClient:
    """Authenticate a confidential client via HTTP Basic (RFC 6749 §2.3.1).

    The ``client_id`` and ``client_secret`` are form-urlencoded before being
    colon-joined and base64'd, so each half is unquoted after the split. Any
    failure — missing header, malformed, unknown client, wrong/absent secret —
    is the same 401 ``invalid_client`` with a Basic challenge, so probing cannot
    distinguish "no such client" from "bad secret".
    """
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        raise invalid_client("client authentication required")
    try:
        decoded = base64.b64decode(header[len("Basic ") :], validate=True).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise invalid_client("malformed Basic credentials") from exc
    client_id, sep, secret = decoded.partition(":")
    if not sep:
        raise invalid_client("malformed Basic credentials")
    client_id, secret = unquote(client_id), unquote(secret)

    client = registry.get(client_id)
    if (
        client is None
        or client.client_secret_hash is None
        or not verify_password(secret, client.client_secret_hash)
    ):
        raise invalid_client("client authentication failed")
    return client


def _resolve_token_client(
    request: Request, form, registry: ClientRegistry
) -> OAuthClient:
    """Identify the client on an authorization_code or refresh_token request.

    A confidential client authenticates via Basic; a public client (SPA/mobile,
    ``token_endpoint_auth_method=none``) presents only its ``client_id`` and
    proves possession through PKCE (auth code) or the unguessable token (refresh).
    A confidential client that fails to authenticate is rejected — body
    ``client_id`` alone never suffices for it.
    """
    if request.headers.get("Authorization", "").startswith("Basic "):
        return _authenticate_client_basic(request, registry)
    client_id = form.get("client_id")
    if not client_id:
        raise invalid_client("client authentication required")
    client = registry.get(client_id)
    if client is None or not client.is_public:
        raise invalid_client("client authentication required")
    return client


def _exchange_authorization_code(
    form,
    client: OAuthClient,
    issuer: str,
    code_store: AuthorizationCodeStore,
    refresh_store: RefreshTokenStore,
) -> dict:
    """Redeem an authorization code for a token (RFC 6749 §4.1.3 + PKCE).

    The code is consumed (single-use) then re-bound to the presenting client,
    the redirect_uri, and the PKCE verifier before any token is minted.
    """
    code = form.get("code")
    if not code:
        raise OAuthError(INVALID_REQUEST, "missing code")
    record = code_store.consume(code)
    if record is None:
        raise OAuthError(INVALID_GRANT, "invalid or expired authorization code")
    if record.client_id != client.client_id:
        raise OAuthError(INVALID_GRANT, "authorization code was not issued to this client")
    if record.redirect_uri != form.get("redirect_uri"):
        raise OAuthError(INVALID_GRANT, "redirect_uri does not match the authorization request")
    verify_pkce(form.get("code_verifier"), record.code_challenge, record.code_challenge_method)
    return issue_authorization_code_token(
        record=record, issuer=issuer, refresh_store=refresh_store
    )


def _refresh(
    form, client: OAuthClient, issuer: str, refresh_store: RefreshTokenStore
) -> dict:
    """Rotate a refresh token for a new token set (RFC 6749 §6)."""
    presented = form.get("refresh_token")
    if not presented:
        raise OAuthError(INVALID_REQUEST, "missing refresh_token")
    # rotate() consumes the token and triggers family revocation on reuse.
    record = refresh_store.rotate(presented)
    if record is None or record.client_id != client.client_id:
        raise OAuthError(INVALID_GRANT, "invalid or expired refresh token")
    return issue_refresh_token_grant(
        record=record,
        requested_scope=form.get("scope"),
        issuer=issuer,
        refresh_store=refresh_store,
    )


def _redirect_with(redirect_uri: str, params: dict[str, str]) -> RedirectResponse:
    """302 to ``redirect_uri`` with ``params`` merged into its query string."""
    sep = "&" if urlparse(redirect_uri).query else "?"
    return RedirectResponse(f"{redirect_uri}{sep}{urlencode(params)}", status_code=302)


def _authorize_error_page(error: str, description: str) -> HTMLResponse:
    """Shown when the redirect_uri is untrusted, so we must NOT redirect.

    Bouncing an error to an unvalidated redirect_uri would turn the AS into an
    open redirector (RFC 6749 §4.1.2.1), so these render in place.
    """
    body = (
        "<!doctype html><html><head><title>Authorization error</title></head>"
        f"<body><h1>Authorization request rejected</h1><p>{error}: {description}</p>"
        "</body></html>"
    )
    return HTMLResponse(body, status_code=400)


def build_oauth_endpoints_router(
    registry: ClientRegistry | None = None,
    code_store: AuthorizationCodeStore | None = None,
    refresh_store: RefreshTokenStore | None = None,
    subject_resolver: SubjectResolver | None = None,
    login_path: str = DEFAULT_LOGIN_PATH,
) -> APIRouter:
    """Assemble the ``/oauth`` router (authorize + token).

    ``registry`` / ``code_store`` / ``refresh_store`` default to the process-wide
    singletons. ``subject_resolver`` resolves the logged-in resource owner
    (default: nobody, so ``/authorize`` redirects to ``login_path``); the webapp
    injects a resolver that reads its session. Tests inject their own.
    """
    router = APIRouter(tags=["oauth"])

    def _resolve_subject(request: Request) -> str | None:
        # Injected resolver wins; otherwise the process-wide one, read per-request
        # so a deployment can wire authentication after the router is built.
        resolver = subject_resolver if subject_resolver is not None else get_subject_resolver()
        return resolver(request)

    def _registry() -> ClientRegistry:
        # Resolved per-request when not injected, so late wiring is seen.
        return registry if registry is not None else get_client_registry()

    def _code_store() -> AuthorizationCodeStore:
        return code_store if code_store is not None else get_authorization_code_store()

    def _refresh_store() -> RefreshTokenStore:
        return refresh_store if refresh_store is not None else get_refresh_token_store()

    @router.get("/oauth/authorize")
    async def authorize(request: Request):
        params = request.query_params
        state = params.get("state")

        # Phase 1 — validate client + redirect_uri. Until redirect_uri is proven
        # to belong to the client, errors render in place (never redirect).
        client_id = params.get("client_id")
        client = _registry().get(client_id) if client_id else None
        if client is None:
            return _authorize_error_page(INVALID_REQUEST, "unknown client_id")
        redirect_uri = params.get("redirect_uri")
        if not redirect_uri or redirect_uri not in client.redirect_uris:
            return _authorize_error_page(INVALID_REQUEST, "invalid redirect_uri")

        # Phase 2 — redirect_uri is trusted; remaining errors bounce back to it.
        try:
            if params.get("response_type") != "code":
                raise OAuthError(UNSUPPORTED_RESPONSE_TYPE, "response_type must be 'code'")
            if GRANT_AUTHORIZATION_CODE not in client.grant_types:
                raise OAuthError(
                    UNAUTHORIZED_CLIENT, "client may not use the authorization_code grant"
                )
            challenge = params.get("code_challenge")
            method = params.get("code_challenge_method")
            if not challenge or method != S256:
                raise OAuthError(
                    INVALID_REQUEST, "a PKCE code_challenge with S256 is required"
                )
            scope = resolve_scope(params.get("scope"), client.scopes)
            resource = validate_resource(params.get("resource"), client)
        except OAuthError as exc:
            error = {"error": exc.error}
            if exc.description:
                error["error_description"] = exc.description
            if state is not None:
                error["state"] = state
            return _redirect_with(redirect_uri, error)

        # Phase 3 — authenticate the resource owner (login is a webapp concern).
        subject = _resolve_subject(request)
        if subject is None:
            return _redirect_with(login_path, {"return_to": str(request.url)})

        # Phase 4 — first-party auto-consent: issue the code and hand it back.
        record = _code_store().issue(
            client_id=client.client_id,
            redirect_uri=redirect_uri,
            subject=subject,
            scope=scope,
            code_challenge=challenge,
            code_challenge_method=method,
            resource=resource,
        )
        granted = {"code": record.code}
        if state is not None:
            granted["state"] = state
        return _redirect_with(redirect_uri, granted)

    @router.post("/oauth/token")
    async def token(request: Request) -> JSONResponse:
        form = await request.form()
        grant_type = form.get("grant_type")
        try:
            if grant_type is None:
                raise OAuthError(INVALID_REQUEST, "missing grant_type")
            issuer = _issuer(request)
            if grant_type == GRANT_CLIENT_CREDENTIALS:
                client = _authenticate_client_basic(request, _registry())
                body = issue_client_credentials(
                    client=client,
                    requested_scope=form.get("scope"),
                    resource=form.get("resource"),
                    issuer=issuer,
                )
            elif grant_type == GRANT_AUTHORIZATION_CODE:
                client = _resolve_token_client(request, form, _registry())
                body = _exchange_authorization_code(
                    form, client, issuer, _code_store(), _refresh_store()
                )
            elif grant_type == GRANT_REFRESH_TOKEN:
                client = _resolve_token_client(request, form, _registry())
                body = _refresh(form, client, issuer, _refresh_store())
            else:
                raise OAuthError(
                    UNSUPPORTED_GRANT_TYPE, f"unsupported grant_type: {grant_type}"
                )
            return JSONResponse(
                body, headers={"Cache-Control": "no-store", "Pragma": "no-cache"}
            )
        except OAuthError as exc:
            return exc.to_response()

    return router


__all__ = ["build_oauth_endpoints_router", "build_oauth_router"]
