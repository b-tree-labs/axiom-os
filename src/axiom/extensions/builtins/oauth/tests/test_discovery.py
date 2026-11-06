# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for the oauth AS discovery + JWKS surface (ADR-082).

A client discovers the node's OAuth 2.1 / OIDC metadata and fetches the public
JWKS to verify ES256 tokens — no shared secret. These are the public
``/.well-known`` endpoints (requires_authz=False).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.oauth.api.routers import build_oauth_router
from axiom.webauth import get_key_store


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(build_oauth_router())
    return TestClient(app)


def test_jwks_serves_public_keys_only() -> None:
    r = _client().get("/.well-known/jwks.json")
    assert r.status_code == 200
    keys = r.json()["keys"]
    assert keys, "JWKS must not be empty"
    for k in keys:
        assert k["kty"] == "EC"
        assert k["alg"] == "ES256"
        assert "d" not in k  # never publish the private scalar
    # the active signing key is discoverable by its kid
    assert any(k["kid"] == get_key_store().active.kid for k in keys)


def test_authorization_server_metadata() -> None:
    md = _client().get("/.well-known/oauth-authorization-server").json()
    assert md["issuer"]
    assert md["jwks_uri"].endswith("/.well-known/jwks.json")
    assert md["code_challenge_methods_supported"] == ["S256"]  # PKCE mandatory
    assert "authorization_code" in md["grant_types_supported"]
    assert "client_credentials" in md["grant_types_supported"]
    assert md["token_endpoint"].endswith("/oauth/token")
    assert md["authorization_endpoint"].endswith("/oauth/authorize")


def test_openid_configuration() -> None:
    md = _client().get("/.well-known/openid-configuration").json()
    assert md["issuer"]
    assert "ES256" in md["id_token_signing_alg_values_supported"]
    assert md["userinfo_endpoint"].endswith("/oauth/userinfo")
    assert "sub" in md["claims_supported"]


def test_issuer_derives_from_request() -> None:
    # No override → issuer is the request base URL (works behind any host).
    md = _client().get("/.well-known/openid-configuration").json()
    assert md["issuer"] == "http://testserver"
    assert md["jwks_uri"] == "http://testserver/.well-known/jwks.json"


def test_issuer_override_env(monkeypatch) -> None:
    monkeypatch.setenv("OAUTH_ISSUER", "https://node.example/")
    md = _client().get("/.well-known/oauth-authorization-server").json()
    assert md["issuer"] == "https://node.example"  # trailing slash trimmed
    assert md["token_endpoint"] == "https://node.example/oauth/token"
