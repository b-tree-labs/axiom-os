# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""OIDC Authorization-Code + PKCE flow (AUTH-R1/R2/R6).

Pure-ish building blocks over an injected HTTP client (``post(url, data)->dict``,
``get(url)->dict``) so the flow is unit-tested against a fake IdP. The
interactive loopback listener + browser launch live in the CLI layer (Phase 2).
"""

from __future__ import annotations

import base64
import json
from typing import Any, Optional
from urllib.parse import urlencode

from axiom.extensions.builtins.auth.providers import IdpConfig


def authorization_url(
    idp: IdpConfig,
    *,
    client_id: str,
    redirect_uri: str,
    scopes: list,
    state: str,
    code_challenge: str,
    nonce: str,
    extra: Optional[dict] = None,
) -> str:
    """Build the IdP authorization URL (PKCE S256, state + nonce)."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        **(extra or {}),
    }
    return idp.authorization_endpoint + "?" + urlencode(params)


def exchange_code(
    http: Any,
    idp: IdpConfig,
    *,
    client_id: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    client_secret: Optional[str] = None,
) -> dict:
    """Exchange an authorization code for tokens (access/refresh/id)."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    if client_secret:
        data["client_secret"] = client_secret
    return http.post(idp.token_endpoint, data)


def refresh(
    http: Any,
    idp: IdpConfig,
    *,
    client_id: str,
    refresh_token: str,
    client_secret: Optional[str] = None,
    scopes: Optional[list] = None,
) -> dict:
    """Exchange a refresh token for a fresh access token (silent renewal)."""
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    if client_secret:
        data["client_secret"] = client_secret
    if scopes:
        data["scope"] = " ".join(scopes)
    return http.post(idp.token_endpoint, data)


def parse_id_token(id_token: str) -> dict:
    """Decode the id_token claims. NOTE: Phase 1 decodes only; JWKS signature
    verification (AUTH-R7) lands in Phase 2 — do not trust these claims for
    authorization until then."""
    try:
        payload = id_token.split(".")[1]
    except IndexError as exc:
        raise ValueError("malformed id_token") from exc
    payload += "=" * (-len(payload) % 4)  # restore base64 padding
    return json.loads(base64.urlsafe_b64decode(payload))


__all__ = ["authorization_url", "exchange_code", "parse_id_token", "refresh"]
