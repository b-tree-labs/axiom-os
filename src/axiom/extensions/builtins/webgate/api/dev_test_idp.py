# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A dev lane's test identity provider: a real OIDC sign-in, served by the lane's own node.

Some acts are signed only with the organisation's sign-in (posture ``sso``): the approvals log is
one. A dev lane's personas sign in by password, so a lane could not exercise them, and forging an
``sso`` posture would write records claiming a sign-in that never happened. Instead the lane's node
serves a small OIDC provider of its own, and the gate signs in through it like any other provider:

- ``GET  /dev-idp/<lane>/.well-known/openid-configuration`` and ``GET /dev-idp/<lane>/jwks``;
- ``GET  /dev-idp/<lane>/authorize`` the sign-in form, for the lane's own gate accounts;
  ``POST`` the same, which answers the redirect with a one-time code (authorization code + PKCE);
- ``POST /dev-idp/<lane>/token`` the code for an RS256 ID token, checked by the gate as any other.

**Only on a dev lane** (:func:`lane_for`): the node names its lane (``AXIOM_DEV_TEST_IDP_LANE``) and
its own address (``AXIOM_DEV_TEST_IDP_URL``); the lane must be in the lane registry, the address
must be loopback over ``http`` at one of that lane's two ports, and every request must come from
loopback. Anywhere else nothing is mounted and the gate's provider refuses sign-in (it fails
closed, never drops the gate). The issuer is named ``dev-test:<lane>``: the session's ``idp`` and so
every record's signer say it was a test sign-in.

The signing key is made in the process and never stored; a restart signs everyone out of it.
"""

from __future__ import annotations

import base64
import html
import json
import os
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

LANE_VAR = "AXIOM_DEV_TEST_IDP_LANE"
URL_VAR = "AXIOM_DEV_TEST_IDP_URL"
#: The one client the provider knows: the lane's own gate, a public client (PKCE, no secret).
CLIENT_ID = "axiom-dev-lane"
PROVIDER = "dev-test"
CODE_TTL_SECONDS = 120
TOKEN_TTL_SECONDS = 300
_LOOPBACK = ("127.0.0.1", "::1", "localhost")
_NO_STORE = {"cache-control": "no-store", "pragma": "no-cache"}

_lock = threading.Lock()
_key: Any = None
_kid = ""
_codes: dict[str, dict[str, Any]] = {}


def reset_for_tests() -> None:
    global _key, _kid
    with _lock:
        _key, _kid = None, ""
        _codes.clear()


def lane_for(env: Mapping[str, str] | None = None) -> str | None:
    """The dev lane this node may serve a test provider for, or None anywhere else."""
    e = os.environ if env is None else env
    lane = (e.get(LANE_VAR) or "").strip()
    url = (e.get(URL_VAR) or "").strip()
    if not lane or not url:
        return None
    parts = urlsplit(url)
    if parts.scheme != "http" or (parts.hostname or "") not in _LOOPBACK or parts.port is None:
        return None
    try:
        from pathlib import Path

        from axiom.extensions.builtins.lane.registry import Registry

        path = e.get("AXIOM_LANES_FILE") or str(Path.home() / ".axi" / "local" / "lanes.json")
        held = Registry(path).get(lane)
    except Exception:  # noqa: BLE001 - an unreadable registry names no lane
        return None
    if held is None or parts.port not in (held.front, held.api):
        return None
    return lane


def base_url(env: Mapping[str, str] | None = None) -> str:
    e = os.environ if env is None else env
    return (e.get(URL_VAR) or "").strip().rstrip("/")


def issuer(lane: str, base: str) -> str:
    return f"{base}/dev-idp/{lane}"


def idp_config(lane: str, base: str):
    """The gate's view of the provider: named ``dev-test:<lane>`` so every record shows it."""
    from axiom.extensions.builtins.auth.providers import IdpConfig

    iss = issuer(lane, base)
    return IdpConfig(
        name=f"{PROVIDER}:{lane}",
        authorization_endpoint=f"{iss}/authorize",
        token_endpoint=f"{iss}/token",
        issuer=iss,
        jwks_uri=f"{iss}/jwks",
        default_scopes=("openid", "profile", "email"),
    )


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _signing_key():
    global _key, _kid
    with _lock:
        if _key is None:
            from cryptography.hazmat.primitives.asymmetric import rsa

            _key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            _kid = f"dev-test-{secrets.token_hex(6)}"
        return _key, _kid


def _jwks() -> dict[str, Any]:
    key, kid = _signing_key()
    n = key.public_key().public_numbers()
    to = lambda i: _b64(i.to_bytes((i.bit_length() + 7) // 8, "big"))  # noqa: E731
    return {"keys": [{"kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid, "n": to(n.n), "e": to(n.e)}]}


def _id_token(claims: dict[str, Any]) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    key, kid = _signing_key()
    head = _b64(json.dumps({"alg": "RS256", "kid": kid, "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{_b64(sig)}"


def _loopback_redirect(uri: str) -> bool:
    parts = urlsplit(uri)
    host = parts.hostname or ""
    loop = host in _LOOPBACK or host.endswith(".localhost")  # a lane's tenant host (RFC 6761)
    return parts.scheme == "http" and loop and parts.path.startswith("/gate/oidc")


def register_dev_test_idp_routes(router: APIRouter, *, store: Callable[[], Any]) -> bool:
    """Mount the provider's routes when this node is a dev lane; answers whether it did."""
    lane = lane_for()
    if lane is None:
        return False
    base = base_url()
    iss = issuer(lane, base)
    prefix = f"/dev-idp/{lane}"

    def far(request: Request) -> Response | None:
        host = request.client.host if request.client else ""
        if host not in _LOOPBACK:
            return JSONResponse({"error": "access_denied", "error_description": "a dev lane's test sign-in answers loopback only"},
                                status_code=403)  # fmt: skip
        return None

    def asked(source: Mapping[str, Any]) -> tuple[dict[str, str], str | None]:
        q = {k: str(source.get(k) or "") for k in ("client_id", "redirect_uri", "response_type", "scope", "state",
                                                    "nonce", "code_challenge", "code_challenge_method")}  # fmt: skip
        if q["client_id"] != CLIENT_ID:
            return q, "unknown client"
        if q["response_type"] != "code":
            return q, "only the authorization code flow is served"
        if q["code_challenge_method"] != "S256" or not q["code_challenge"]:
            return q, "PKCE (S256) is required"
        if not _loopback_redirect(q["redirect_uri"]):
            return q, "the redirect must be this lane's own gate, on loopback"
        return q, None

    def form(q: dict[str, str], *, problem: str = "", status: int = 200) -> HTMLResponse:
        hidden = "".join(f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v, quote=True)}">'
                         for k, v in q.items())  # fmt: skip
        note = f'<p role="alert">{html.escape(problem)}</p>' if problem else ""
        page = (
            "<!doctype html><html><head><meta charset='utf-8'><title>Test sign-in</title>"
            "<meta name='viewport' content='width=device-width'></head><body><main>"
            f"<h1>Test sign-in for lane {html.escape(lane)}</h1>"
            "<p>This is a dev lane's test sign-in, not your organisation's. Everything you sign "
            f"here records the issuer <code>{PROVIDER}:{html.escape(lane)}</code>.</p>{note}"
            f'<form method="post" action="{prefix}/authorize">{hidden}'
            '<label>Email <input name="email" type="email" autocomplete="username" required></label>'
            '<label>Password <input name="password" type="password" autocomplete="current-password" required></label>'
            '<button type="submit">Sign in</button></form></main></body></html>'
        )
        return HTMLResponse(page, status_code=status, headers=_NO_STORE)

    @router.get(f"{prefix}/.well-known/openid-configuration")
    async def discovery(request: Request) -> Response:
        return far(request) or JSONResponse({
            "issuer": iss, "authorization_endpoint": f"{iss}/authorize", "token_endpoint": f"{iss}/token",
            "jwks_uri": f"{iss}/jwks", "response_types_supported": ["code"], "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"], "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"], "scopes_supported": ["openid", "profile", "email"],
        })  # fmt: skip

    @router.get(f"{prefix}/jwks")
    async def jwks(request: Request) -> Response:
        return far(request) or JSONResponse(_jwks())

    @router.get(f"{prefix}/authorize")
    async def authorize_page(request: Request) -> Response:
        refused = far(request)
        if refused:
            return refused
        q, problem = asked(request.query_params)
        return form(q, problem=problem or "", status=400 if problem else 200)

    @router.post(f"{prefix}/authorize")
    async def authorize(request: Request) -> Response:
        refused = far(request)
        if refused:
            return refused
        data = dict(await request.form())
        q, problem = asked(data)
        if problem:
            return form(q, problem=problem, status=400)
        from axiom.webauth.users import authenticate

        user = authenticate(store(), str(data.get("email") or ""), str(data.get("password") or ""))
        if user is None:
            return form(q, problem="That email and password do not match an account on this lane.", status=401)
        code = secrets.token_urlsafe(24)
        with _lock:
            now = time.time()
            for c in [c for c, v in _codes.items() if v["exp"] < now]:
                _codes.pop(c, None)
            _codes[code] = {"q": q, "exp": now + CODE_TTL_SECONDS, "user": user, "auth_time": int(now)}
        back = q["redirect_uri"] + ("&" if "?" in q["redirect_uri"] else "?") + urlencode({"code": code, "state": q["state"]})
        return RedirectResponse(back, status_code=303, headers=_NO_STORE)

    @router.post(f"{prefix}/token")
    async def token(request: Request) -> Response:
        refused = far(request)
        if refused:
            return refused
        from axiom.extensions.builtins.auth.pkce import challenge_for

        data = dict(await request.form())

        def bad(why: str) -> JSONResponse:
            return JSONResponse({"error": "invalid_grant", "error_description": why}, status_code=400, headers=_NO_STORE)

        with _lock:
            held = _codes.pop(str(data.get("code") or ""), None)  # a code is spent by its first use
        if held is None or held["exp"] < time.time():
            return bad("unknown, spent or expired code")
        q = held["q"]
        if data.get("grant_type") != "authorization_code" or data.get("client_id") != CLIENT_ID:
            return bad("wrong grant or client")
        if data.get("redirect_uri") != q["redirect_uri"]:
            return bad("the redirect does not match the one the code was issued for")
        if challenge_for(str(data.get("code_verifier") or "")) != q["code_challenge"]:
            return bad("the PKCE verifier does not match")
        user = held["user"]
        now = int(time.time())
        claims = {
            "iss": iss, "aud": CLIENT_ID, "sub": user.user_id, "email": user.email, "email_verified": True,
            "name": user.name or user.email, "nonce": q["nonce"] or None, "iat": now, "exp": now + TOKEN_TTL_SECONDS,
            "auth_time": held["auth_time"], "amr": ["pwd"], "test_sign_in": True,
        }  # fmt: skip
        return JSONResponse({"id_token": _id_token({k: v for k, v in claims.items() if v is not None}),
                             "access_token": secrets.token_urlsafe(24), "token_type": "Bearer",
                             "expires_in": TOKEN_TTL_SECONDS}, headers=_NO_STORE)  # fmt: skip

    return True


__all__ = ["CLIENT_ID", "LANE_VAR", "PROVIDER", "URL_VAR", "idp_config", "lane_for", "register_dev_test_idp_routes"]
