# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``grant_type=refresh_token`` and refresh issuance on the code exchange."""

from __future__ import annotations

import base64
import warnings

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.oauth.api.routers import build_oauth_endpoints_router
from axiom.extensions.builtins.oauth.clients import InMemoryClientRegistry
from axiom.extensions.builtins.oauth.codes import InMemoryAuthorizationCodeStore
from axiom.extensions.builtins.oauth.models import (
    AUTH_METHOD_NONE,
    CLIENT_SECRET_BASIC,
    OAuthClient,
)
from axiom.extensions.builtins.oauth.pkce import S256, compute_s256_challenge
from axiom.extensions.builtins.oauth.refresh import InMemoryRefreshTokenStore
from axiom.webauth import get_password_hash, verify_token
from axiom.webauth.keys import reset_key_store_for_tests

warnings.filterwarnings("ignore")

ISSUER = "https://as.example"
REDIRECT = "https://app.example/cb"
VERIFIER = "refresh-verifier-" + "p" * 40
SECRET = "conf-secret-1111111111111111111"


@pytest.fixture(autouse=True)
def _fresh_keys():
    reset_key_store_for_tests()
    yield
    reset_key_store_for_tests()


def _public_client():
    return OAuthClient(
        client_id="spa",
        grant_types=("authorization_code",),
        scopes=("memory.read", "offline_access"),
        audiences=("https://api.example",),
        redirect_uris=(REDIRECT,),
        token_endpoint_auth_method=AUTH_METHOD_NONE,
    )


def _confidential_client():
    return OAuthClient(
        client_id="web",
        client_secret_hash=get_password_hash(SECRET),
        grant_types=("authorization_code",),
        scopes=("memory.read", "offline_access"),
        redirect_uris=(REDIRECT,),
        token_endpoint_auth_method=CLIENT_SECRET_BASIC,
    )


def _harness(clients):
    registry = InMemoryClientRegistry(clients)
    codes = InMemoryAuthorizationCodeStore()
    refresh = InMemoryRefreshTokenStore()
    app = FastAPI()
    app.include_router(
        build_oauth_endpoints_router(
            registry=registry, code_store=codes, refresh_store=refresh
        )
    )
    return TestClient(app, base_url=ISSUER), codes


def _seed(codes, *, client_id, scope):
    return codes.issue(
        client_id=client_id, redirect_uri=REDIRECT, subject="user:alice",
        scope=scope, code_challenge=compute_s256_challenge(VERIFIER),
        code_challenge_method=S256, resource="https://api.example",
    )


def _basic(cid, secret):
    return {"Authorization": "Basic " + base64.b64encode(f"{cid}:{secret}".encode()).decode()}


def _exchange(c, code, *, client_id="spa", headers=None):
    return c.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "client_id": client_id, "code_verifier": VERIFIER}, headers=headers or {})


def test_code_exchange_issues_refresh_only_with_offline_access():
    c, codes = _harness([_public_client()])
    # No offline_access -> no refresh token.
    r1 = _exchange(c, _seed(codes, client_id="spa", scope="memory.read").code)
    assert r1.status_code == 200
    assert "refresh_token" not in r1.json()
    # offline_access -> refresh token present.
    r2 = _exchange(c, _seed(codes, client_id="spa", scope="memory.read offline_access").code)
    assert "refresh_token" in r2.json()


def test_refresh_grant_mints_new_access_and_rotates_refresh():
    c, codes = _harness([_public_client()])
    rt = _exchange(c, _seed(codes, client_id="spa", scope="memory.read offline_access").code).json()["refresh_token"]
    r = c.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": rt, "client_id": "spa"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert verify_token(body["access_token"], audience="https://api.example", issuer=ISSUER) is not None
    # Rotation: a new refresh token, different from the presented one.
    assert body["refresh_token"] != rt


def test_refresh_scope_cannot_widen():
    c, codes = _harness([_public_client()])
    rt = _exchange(c, _seed(codes, client_id="spa", scope="memory.read offline_access").code).json()["refresh_token"]
    r = c.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": rt, "client_id": "spa",
        "scope": "memory.read memory.write"})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_scope"


def test_refresh_reuse_is_detected_and_revokes_family():
    c, codes = _harness([_public_client()])
    rt0 = _exchange(c, _seed(codes, client_id="spa", scope="memory.read offline_access").code).json()["refresh_token"]
    rt1 = c.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": rt0, "client_id": "spa"}).json()["refresh_token"]
    # Replaying the rotated-away token is rejected...
    replay = c.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": rt0, "client_id": "spa"})
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"
    # ...and the whole family is revoked, so the fresh token is dead too.
    after = c.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": rt1, "client_id": "spa"})
    assert after.status_code == 400


def test_refresh_confidential_client_must_authenticate():
    c, codes = _harness([_confidential_client()])
    rt = _exchange(c, _seed(codes, client_id="web", scope="memory.read offline_access").code,
                   headers=_basic("web", SECRET)).json()["refresh_token"]
    # Present refresh with only body client_id (no Basic) -> rejected.
    bad = c.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": rt, "client_id": "web"})
    assert bad.status_code == 401
    # With Basic -> ok.
    good = c.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": rt}, headers=_basic("web", SECRET))
    assert good.status_code == 200, good.text


def test_refresh_missing_token_is_invalid_request():
    c, _ = _harness([_public_client()])
    r = c.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": "spa"})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


def test_refresh_unknown_token_is_invalid_grant():
    c, _ = _harness([_public_client()])
    r = c.post("/oauth/token", data={
        "grant_type": "refresh_token", "refresh_token": "nope", "client_id": "spa"})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"
