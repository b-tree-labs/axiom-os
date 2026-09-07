# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``GET /oauth/authorize`` — the code-issuance half of the auth-code flow.

Validates the request, authenticates the resource owner (an injected resolver —
the login page is a webapp concern), then issues a PKCE-bound authorization code
and 302-redirects it back. Includes the full round-trip: authorize issues a code,
the token endpoint redeems it.
"""

from __future__ import annotations

import warnings
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.oauth.api.routers import build_oauth_endpoints_router
from axiom.extensions.builtins.oauth.clients import InMemoryClientRegistry
from axiom.extensions.builtins.oauth.codes import InMemoryAuthorizationCodeStore
from axiom.extensions.builtins.oauth.models import AUTH_METHOD_NONE, OAuthClient
from axiom.extensions.builtins.oauth.pkce import S256, compute_s256_challenge
from axiom.webauth import verify_token
from axiom.webauth.keys import reset_key_store_for_tests

warnings.filterwarnings("ignore")

ISSUER = "https://as.example"
REDIRECT = "https://app.example/cb"
VERIFIER = "authz-verifier-" + "q" * 40
CHALLENGE = compute_s256_challenge(VERIFIER)


@pytest.fixture(autouse=True)
def _fresh_keys():
    reset_key_store_for_tests()
    yield
    reset_key_store_for_tests()


def _client_rec() -> OAuthClient:
    return OAuthClient(
        client_id="spa",
        grant_types=("authorization_code",),
        scopes=("memory.read", "memory.write"),
        audiences=("https://api.example",),
        redirect_uris=(REDIRECT,),
        token_endpoint_auth_method=AUTH_METHOD_NONE,
    )


def _harness(subject="user:alice", clients=None):
    registry = InMemoryClientRegistry(clients or [_client_rec()])
    store = InMemoryAuthorizationCodeStore()
    app = FastAPI()
    app.include_router(
        build_oauth_endpoints_router(
            registry=registry,
            code_store=store,
            subject_resolver=lambda request: subject,
        )
    )
    return TestClient(app, base_url=ISSUER, follow_redirects=False), store


def _authorize(client, **overrides):
    params = {
        "response_type": "code",
        "client_id": "spa",
        "redirect_uri": REDIRECT,
        "scope": "memory.read",
        "state": "xyz",
        "code_challenge": CHALLENGE,
        "code_challenge_method": S256,
        "resource": "https://api.example",
    }
    params.update(overrides)
    params = {k: v for k, v in params.items() if v is not None}
    return client.get("/oauth/authorize", params=params)


def _loc(resp):
    return urlparse(resp.headers["location"])


def test_happy_path_redirects_with_code_and_state():
    c, store = _harness()
    r = _authorize(c)
    assert r.status_code == 302
    loc = _loc(r)
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == REDIRECT
    q = parse_qs(loc.query)
    assert q["state"] == ["xyz"]
    assert "code" in q


def test_issued_code_round_trips_through_token_endpoint():
    c, store = _harness()
    r = _authorize(c)
    code = parse_qs(_loc(r).query)["code"][0]
    # Redeem it at the token endpoint with the matching verifier.
    tok = c.post(
        "/oauth/token",
        data={"grant_type": "authorization_code", "code": code,
              "redirect_uri": REDIRECT, "client_id": "spa", "code_verifier": VERIFIER},
    )
    assert tok.status_code == 200, tok.text
    claims = verify_token(tok.json()["access_token"], audience="https://api.example", issuer=ISSUER)
    assert claims is not None
    assert claims["sub"] == "user:alice"
    assert claims["scope"] == "memory.read"


def test_unauthenticated_redirects_to_login_with_return_to():
    c, _ = _harness(subject=None)
    r = _authorize(c)
    assert r.status_code == 302
    loc = _loc(r)
    assert loc.path == "/login"
    q = parse_qs(loc.query)
    assert "return_to" in q
    assert "/oauth/authorize" in q["return_to"][0]


def test_unknown_client_shows_error_page_not_redirect():
    c, _ = _harness()
    r = _authorize(c, client_id="ghost")
    assert r.status_code == 400
    assert "location" not in {k.lower() for k in r.headers}


def test_bad_redirect_uri_shows_error_page_not_redirect():
    c, _ = _harness()
    r = _authorize(c, redirect_uri="https://evil.example/cb")
    assert r.status_code == 400
    assert "location" not in {k.lower() for k in r.headers}


def test_wrong_response_type_redirects_error():
    c, _ = _harness()
    r = _authorize(c, response_type="token")
    assert r.status_code == 302
    q = parse_qs(_loc(r).query)
    assert q["error"] == ["unsupported_response_type"]
    assert q["state"] == ["xyz"]


def test_missing_pkce_redirects_invalid_request():
    c, _ = _harness()
    r = _authorize(c, code_challenge=None, code_challenge_method=None)
    assert r.status_code == 302
    assert parse_qs(_loc(r).query)["error"] == ["invalid_request"]


def test_plain_pkce_method_rejected():
    c, _ = _harness()
    r = _authorize(c, code_challenge_method="plain")
    assert r.status_code == 302
    assert parse_qs(_loc(r).query)["error"] == ["invalid_request"]


def test_disallowed_scope_redirects_invalid_scope():
    c, _ = _harness()
    r = _authorize(c, scope="memory.read admin.super")
    assert r.status_code == 302
    assert parse_qs(_loc(r).query)["error"] == ["invalid_scope"]


def test_disallowed_resource_redirects_invalid_target():
    c, _ = _harness()
    r = _authorize(c, resource="https://evil.example")
    assert r.status_code == 302
    assert parse_qs(_loc(r).query)["error"] == ["invalid_target"]


def test_process_wide_resolver_is_used_when_none_injected():
    # Mirrors the mount path: the router is built with no resolver, and a
    # deployment wires one later via set_subject_resolver().
    from axiom.extensions.builtins.oauth.api.routers import (
        reset_subject_resolver_for_tests,
        set_subject_resolver,
    )
    from axiom.extensions.builtins.oauth.clients import set_client_registry
    from axiom.extensions.builtins.oauth.codes import set_authorization_code_store

    set_client_registry(InMemoryClientRegistry([_client_rec()]))
    set_authorization_code_store(InMemoryAuthorizationCodeStore())
    reset_subject_resolver_for_tests()
    try:
        app = FastAPI()
        app.include_router(build_oauth_endpoints_router())  # no resolver injected
        c = TestClient(app, base_url=ISSUER, follow_redirects=False)
        # Nobody wired yet -> login redirect.
        assert _loc(_authorize(c)).path == "/login"
        # Wire a resolver after the router is built -> authorize now completes.
        set_subject_resolver(lambda request: "user:bob")
        assert "code" in parse_qs(_loc(_authorize(c)).query)
    finally:
        reset_subject_resolver_for_tests()
        from axiom.extensions.builtins.oauth.clients import (
            reset_client_registry_for_tests,
        )
        from axiom.extensions.builtins.oauth.codes import (
            reset_authorization_code_store_for_tests,
        )

        reset_client_registry_for_tests()
        reset_authorization_code_store_for_tests()


def test_client_without_authz_code_grant_redirects_unauthorized_client():
    only_cc = OAuthClient(
        client_id="spa",
        grant_types=("client_credentials",),
        scopes=("memory.read",),
        redirect_uris=(REDIRECT,),
        token_endpoint_auth_method=AUTH_METHOD_NONE,
    )
    c, _ = _harness(clients=[only_cc])
    r = _authorize(c)
    assert r.status_code == 302
    assert parse_qs(_loc(r).query)["error"] == ["unauthorized_client"]
