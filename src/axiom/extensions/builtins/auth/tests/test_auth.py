# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""OIDC Phase-1 core: PKCE, providers, the auth-code flow, and the token source
— all against a fake IdP HTTP client (no network)."""

from __future__ import annotations

import base64
import json

import pytest

from axiom.extensions.builtins.auth import flow, pkce, providers
from axiom.extensions.builtins.auth.token_source import TokenSource


class _FakeHttp:
    def __init__(self, post_responses=None, get_response=None):
        self._posts = list(post_responses or [])
        self._get = get_response
        self.posts = []

    def post(self, url, data):
        self.posts.append((url, data))
        return self._posts.pop(0)

    def get(self, url):
        return self._get


def _jwt(claims: dict) -> str:
    def seg(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'none'})}.{seg(claims)}.sig"


# --- PKCE (AUTH-R1) ---

def test_pkce_challenge_is_deterministic_sha256():
    v, c = pkce.generate_pkce()
    assert pkce.challenge_for(v) == c
    assert "=" not in v and "=" not in c     # base64url, unpadded
    assert pkce.generate_pkce()[0] != pkce.generate_pkce()[0]  # fresh entropy


# --- providers (AUTH-R5) ---

def test_entra_is_tenant_scoped():
    idp = providers.entra("example-tenant-id")
    assert "example-tenant-id" in idp.authorization_endpoint
    assert idp.token_endpoint.endswith("/oauth2/v2.0/token")
    assert idp.issuer.endswith("/v2.0") and idp.jwks_uri
    assert "offline_access" in idp.default_scopes


def test_from_discovery_reads_well_known():
    http = _FakeHttp(get_response={
        "issuer": "https://idp.example",
        "authorization_endpoint": "https://idp.example/auth",
        "token_endpoint": "https://idp.example/token",
        "jwks_uri": "https://idp.example/jwks",
    })
    idp = providers.from_discovery(http, "https://idp.example")
    assert idp.authorization_endpoint == "https://idp.example/auth"
    assert idp.token_endpoint == "https://idp.example/token"


# --- flow (AUTH-R1/R2) ---

def test_authorization_url_has_pkce_state_nonce():
    idp = providers.google()
    url = flow.authorization_url(
        idp, client_id="cid", redirect_uri="http://127.0.0.1:9000/cb",
        scopes=["openid", "email"], state="st-123", code_challenge="chal", nonce="n-9",
    )
    assert url.startswith(idp.authorization_endpoint + "?")
    for fragment in ("client_id=cid", "code_challenge=chal",
                     "code_challenge_method=S256", "state=st-123", "nonce=n-9",
                     "scope=openid+email", "response_type=code"):
        assert fragment in url


def test_exchange_code_posts_pkce_and_returns_tokens():
    idp = providers.google()
    http = _FakeHttp(post_responses=[{
        "access_token": "at", "refresh_token": "rt", "id_token": _jwt({"sub": "u1"}),
        "expires_in": 3600,
    }])
    tok = flow.exchange_code(http, idp, client_id="cid", code="abc",
                             redirect_uri="http://127.0.0.1:9000/cb", code_verifier="ver")
    assert tok["access_token"] == "at"
    _url, data = http.posts[0]
    assert data["grant_type"] == "authorization_code" and data["code_verifier"] == "ver"


def test_parse_id_token_claims():
    claims = flow.parse_id_token(_jwt({"sub": "eid-123", "email": "user@example.org"}))
    assert claims["sub"] == "eid-123" and claims["email"] == "user@example.org"
    with pytest.raises(ValueError):
        flow.parse_id_token("not-a-jwt")


# --- token source (AUTH-R4) ---

class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_token_source_caches_then_refreshes_on_expiry():
    idp = providers.google()
    http = _FakeHttp(post_responses=[
        {"access_token": "at-1", "expires_in": 3600},
        {"access_token": "at-2", "expires_in": 3600},
    ])
    clock = _Clock(1000.0)
    ts = TokenSource(http, idp, client_id="cid", refresh_token="rt", now=clock, skew_seconds=60)

    assert ts() == "at-1"            # first call refreshes
    assert ts() == "at-1"            # cached — still valid, no new POST
    assert len(http.posts) == 1

    clock.t += 3600                  # past expiry
    assert ts() == "at-2"            # refreshed again
    assert len(http.posts) == 2


def test_token_source_honors_refresh_rotation():
    idp = providers.google()
    http = _FakeHttp(post_responses=[
        {"access_token": "at-1", "refresh_token": "rt-2", "expires_in": 1},
        {"access_token": "at-2", "expires_in": 1},
    ])
    clock = _Clock(1000.0)
    ts = TokenSource(http, idp, client_id="cid", refresh_token="rt-1", now=clock, skew_seconds=0)
    ts()                              # uses rt-1, gets rotated rt-2
    clock.t += 10
    ts()                              # should refresh with rt-2
    assert http.posts[1][1]["refresh_token"] == "rt-2"


# --- Open IdP registry (ADR-075 §2; parity with directory/calendar/secrets) ---

def test_idp_registry_resolves_builtins():
    entra = providers.get_idp("entra", tenant="t-123")
    assert entra.name == "entra" and "t-123" in entra.token_endpoint
    google = providers.get_idp("google")
    assert google.name == "google"


def test_idp_registry_lists_available_sorted():
    names = providers.available_idps()
    assert "entra" in names and "google" in names
    assert list(names) == sorted(names)


def test_unknown_idp_raises_listing_available():
    with pytest.raises(ValueError, match="unknown idp 'okta'.*available"):
        providers.get_idp("okta")


def test_entra_without_tenant_is_a_clear_error():
    with pytest.raises(ValueError, match="tenant"):
        providers.get_idp("entra")


def test_new_idp_registers_without_touching_callers():
    def _okta(**config):
        base = config["issuer"].rstrip("/")
        return providers.IdpConfig(name="okta",
                                   authorization_endpoint=f"{base}/authorize",
                                   token_endpoint=f"{base}/token", issuer=base)
    providers.register_idp("okta", _okta)
    try:
        idp = providers.get_idp("okta", issuer="https://example.okta.com")
        assert idp.name == "okta" and idp.token_endpoint.endswith("/token")
        assert "okta" in providers.available_idps()
    finally:
        providers._IDP_REGISTRY.pop("okta", None)
