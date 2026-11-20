# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``POST /oauth/token`` — the client_credentials grant (ADR-082, build P2).

The machine-to-machine path: an agent / MCP client authenticates with its own
credentials and gets back an audience-bound ES256 access token that any resource
server verifies from the public JWKS — no shared secret with the RS. These tests
drive the grant end-to-end through the router and assert the RFC 6749 / RFC 8707
error surface.
"""

from __future__ import annotations

import base64
import warnings

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.oauth.api.routers import build_oauth_endpoints_router
from axiom.extensions.builtins.oauth.clients import InMemoryClientRegistry
from axiom.extensions.builtins.oauth.models import OAuthClient
from axiom.webauth import get_password_hash, verify_token
from axiom.webauth.keys import reset_key_store_for_tests

warnings.filterwarnings("ignore")

SECRET = "s3cr3t-high-entropy-value-000000"
ISSUER = "https://axiom.example"


@pytest.fixture(autouse=True)
def _fresh_keys():
    reset_key_store_for_tests()
    yield
    reset_key_store_for_tests()


def _registry(**overrides) -> InMemoryClientRegistry:
    fields = {
        "client_id": "svc-agent",
        "client_secret_hash": get_password_hash(SECRET),
        "grant_types": ("client_credentials",),
        "scopes": ("memory.read", "memory.write"),
        "audiences": ("https://api.axiom.example",),
        **overrides,
    }
    return InMemoryClientRegistry([OAuthClient(**fields)])


def _client(registry: InMemoryClientRegistry) -> TestClient:
    app = FastAPI()
    app.include_router(build_oauth_endpoints_router(registry=registry))
    # Bind a deterministic issuer so aud/iss assertions are stable.
    return TestClient(app, base_url=ISSUER)


def _basic(client_id: str, secret: str) -> dict:
    raw = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def _post(client: TestClient, data: dict, headers: dict | None = None):
    return client.post("/oauth/token", data=data, headers=headers or {})


def test_happy_path_issues_audience_bound_token():
    c = _client(_registry())
    r = _post(
        c,
        {"grant_type": "client_credentials", "scope": "memory.read",
         "resource": "https://api.axiom.example"},
        _basic("svc-agent", SECRET),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "Bearer"
    assert body["scope"] == "memory.read"
    assert body["expires_in"] > 0
    # No-store per RFC 6749 §5.1.
    assert r.headers["Cache-Control"] == "no-store"

    claims = verify_token(
        body["access_token"], audience="https://api.axiom.example", issuer=ISSUER
    )
    assert claims is not None, "token must verify against the JWKS"
    assert claims["sub"] == "svc-agent"
    assert claims["client_id"] == "svc-agent"
    assert claims["scope"] == "memory.read"


def test_scope_defaults_to_all_client_scopes_when_omitted():
    c = _client(_registry())
    r = _post(
        c,
        {"grant_type": "client_credentials", "resource": "https://api.axiom.example"},
        _basic("svc-agent", SECRET),
    )
    assert r.status_code == 200, r.text
    assert set(r.json()["scope"].split()) == {"memory.read", "memory.write"}


def test_bad_secret_is_invalid_client_401():
    c = _client(_registry())
    r = _post(
        c,
        {"grant_type": "client_credentials"},
        _basic("svc-agent", "wrong-secret"),
    )
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_client"
    assert r.headers.get("WWW-Authenticate", "").startswith("Basic")


def test_unknown_client_is_invalid_client_401():
    c = _client(_registry())
    r = _post(c, {"grant_type": "client_credentials"}, _basic("nope", SECRET))
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_client"


def test_missing_auth_is_invalid_client_401():
    c = _client(_registry())
    r = _post(c, {"grant_type": "client_credentials"})
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_client"


def test_disallowed_scope_is_invalid_scope():
    c = _client(_registry())
    r = _post(
        c,
        {"grant_type": "client_credentials", "scope": "memory.read admin.super"},
        _basic("svc-agent", SECRET),
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_scope"


def test_disallowed_resource_is_invalid_target():
    c = _client(_registry())
    r = _post(
        c,
        {"grant_type": "client_credentials", "resource": "https://evil.example"},
        _basic("svc-agent", SECRET),
    )
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_target"


def test_grant_not_allowed_for_client_is_unauthorized_client():
    # Client registered without client_credentials in its grant set.
    reg = _registry(grant_types=("authorization_code",))
    c = _client(reg)
    r = _post(
        c,
        {"grant_type": "client_credentials"},
        _basic("svc-agent", SECRET),
    )
    assert r.status_code == 400
    assert r.json()["error"] == "unauthorized_client"


def test_unsupported_grant_type():
    c = _client(_registry())
    r = _post(
        c,
        {"grant_type": "password", "username": "x", "password": "y"},
        _basic("svc-agent", SECRET),
    )
    assert r.status_code == 400
    assert r.json()["error"] == "unsupported_grant_type"


def test_missing_grant_type_is_invalid_request():
    c = _client(_registry())
    r = _post(c, {"scope": "memory.read"}, _basic("svc-agent", SECRET))
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


def test_resource_omitted_defaults_audience_to_issuer():
    c = _client(_registry())
    r = _post(
        c,
        {"grant_type": "client_credentials", "scope": "memory.read"},
        _basic("svc-agent", SECRET),
    )
    assert r.status_code == 200, r.text
    claims = verify_token(r.json()["access_token"], audience=ISSUER, issuer=ISSUER)
    assert claims is not None
