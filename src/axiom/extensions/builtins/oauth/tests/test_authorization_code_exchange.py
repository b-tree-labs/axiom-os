# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``grant_type=authorization_code`` exchange on ``/oauth/token`` (ADR-082, P2).

The redemption half of the web/mobile path: a code issued by ``/oauth/authorize``
(next cut) is exchanged for an access token, gated by PKCE S256 and by exact
client_id + redirect_uri binding. Public clients (SPA/mobile) present only their
client_id — PKCE is the possession proof; confidential clients authenticate too.
"""

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
from axiom.webauth import get_password_hash, verify_token
from axiom.webauth.keys import reset_key_store_for_tests

warnings.filterwarnings("ignore")

ISSUER = "https://as.example"
REDIRECT = "https://app.example/cb"
VERIFIER = "verifier-" + "z" * 48  # >= 43 chars
SECRET = "conf-secret-0000000000000000000"


@pytest.fixture(autouse=True)
def _fresh_keys():
    reset_key_store_for_tests()
    yield
    reset_key_store_for_tests()


def _public_client() -> OAuthClient:
    return OAuthClient(
        client_id="spa",
        grant_types=("authorization_code",),
        scopes=("memory.read",),
        audiences=("https://api.example",),
        redirect_uris=(REDIRECT,),
        token_endpoint_auth_method=AUTH_METHOD_NONE,
    )


def _confidential_client() -> OAuthClient:
    return OAuthClient(
        client_id="web",
        client_secret_hash=get_password_hash(SECRET),
        grant_types=("authorization_code",),
        scopes=("memory.read",),
        redirect_uris=(REDIRECT,),
        token_endpoint_auth_method=CLIENT_SECRET_BASIC,
    )


def _harness(clients):
    registry = InMemoryClientRegistry(clients)
    store = InMemoryAuthorizationCodeStore()
    app = FastAPI()
    app.include_router(
        build_oauth_endpoints_router(registry=registry, code_store=store)
    )
    return TestClient(app, base_url=ISSUER), store


def _seed_code(store, *, client_id, subject="user:alice", scope="memory.read",
               redirect_uri=REDIRECT, resource="https://api.example",
               verifier=VERIFIER):
    return store.issue(
        client_id=client_id,
        redirect_uri=redirect_uri,
        subject=subject,
        scope=scope,
        code_challenge=compute_s256_challenge(verifier),
        code_challenge_method=S256,
        resource=resource,
    )


def _basic(cid, secret):
    return {"Authorization": "Basic " + base64.b64encode(f"{cid}:{secret}".encode()).decode()}


def _exchange(client, code, *, client_id=None, verifier=VERIFIER,
              redirect_uri=REDIRECT, headers=None):
    data = {"grant_type": "authorization_code", "code": code,
            "redirect_uri": redirect_uri, "code_verifier": verifier}
    if client_id is not None:
        data["client_id"] = client_id
    return client.post("/oauth/token", data=data, headers=headers or {})


def test_public_client_exchanges_code_for_token():
    c, store = _harness([_public_client()])
    rec = _seed_code(store, client_id="spa")
    r = _exchange(c, rec.code, client_id="spa")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token_type"] == "Bearer"
    claims = verify_token(body["access_token"], audience="https://api.example", issuer=ISSUER)
    assert claims is not None
    assert claims["sub"] == "user:alice"
    assert claims["client_id"] == "spa"
    assert claims["scope"] == "memory.read"


def test_confidential_client_must_authenticate():
    c, store = _harness([_confidential_client()])
    rec = _seed_code(store, client_id="web")
    # Presenting client_id in the body without Basic is not enough for a conf client.
    r = _exchange(c, rec.code, client_id="web")
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_client"


def test_confidential_client_with_basic_succeeds():
    c, store = _harness([_confidential_client()])
    rec = _seed_code(store, client_id="web")
    r = _exchange(c, rec.code, headers=_basic("web", SECRET))
    assert r.status_code == 200, r.text


def test_wrong_verifier_is_invalid_grant():
    c, store = _harness([_public_client()])
    rec = _seed_code(store, client_id="spa")
    r = _exchange(c, rec.code, client_id="spa", verifier="wrong-" + "y" * 48)
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


def test_redirect_uri_mismatch_is_invalid_grant():
    c, store = _harness([_public_client()])
    rec = _seed_code(store, client_id="spa")
    r = _exchange(c, rec.code, client_id="spa", redirect_uri="https://app.example/other")
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


def test_code_replay_is_rejected():
    c, store = _harness([_public_client()])
    rec = _seed_code(store, client_id="spa")
    assert _exchange(c, rec.code, client_id="spa").status_code == 200
    replay = _exchange(c, rec.code, client_id="spa")
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"


def test_code_bound_to_other_client_is_invalid_grant():
    c, store = _harness([_public_client()])
    rec = _seed_code(store, client_id="someone-else")
    r = _exchange(c, rec.code, client_id="spa")
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


def test_unknown_code_is_invalid_grant():
    c, _ = _harness([_public_client()])
    r = _exchange(c, "no-such-code", client_id="spa")
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_grant"


def test_missing_code_is_invalid_request():
    c, _ = _harness([_public_client()])
    r = c.post("/oauth/token", data={"grant_type": "authorization_code",
                                     "client_id": "spa", "redirect_uri": REDIRECT,
                                     "code_verifier": VERIFIER})
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"
