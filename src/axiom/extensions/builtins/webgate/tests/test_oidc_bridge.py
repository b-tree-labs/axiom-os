# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Proof of the OIDC fast-follow seam: one webgate login → oauth /authorize.

A browser that logged in at the webgate carries a session cookie. With the bridge
resolver wired into the oauth AS, that same cookie satisfies /oauth/authorize —
no second login. This is the concrete guarantee that adding OIDC is additive.
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
from axiom.extensions.builtins.webgate.api.routers import build_webgate_router
from axiom.extensions.builtins.webgate.bridge import session_subject_resolver
from axiom.webauth import get_password_hash
from axiom.webauth.keys import reset_key_store_for_tests
from axiom.webauth.users import InMemoryUserStore, User

warnings.filterwarnings("ignore")

PW = "pw-000000000000000"
REDIRECT = "https://app.example/cb"
VERIFIER = "bridge-verifier-" + "z" * 40


@pytest.fixture(autouse=True)
def _keys():
    reset_key_store_for_tests()
    yield
    reset_key_store_for_tests()


def _app():
    users = InMemoryUserStore([
        User(user_id="u1", email="alice@example.org", password_hash=get_password_hash(PW))
    ])
    clients = InMemoryClientRegistry([OAuthClient(
        client_id="spa", grant_types=("authorization_code",), scopes=("openid",),
        redirect_uris=(REDIRECT,), token_endpoint_auth_method=AUTH_METHOD_NONE)])
    app = FastAPI()
    app.include_router(build_webgate_router(users, secure_cookies=False))
    app.include_router(build_oauth_endpoints_router(
        registry=clients, code_store=InMemoryAuthorizationCodeStore(),
        subject_resolver=session_subject_resolver,  # the bridge
        login_path="/gate/login",  # unauth -> the webgate login (not the default /login)
    ))
    return TestClient(app, base_url="http://gate.example", follow_redirects=False)


def _authorize(c):
    return c.get("/oauth/authorize", params={
        "response_type": "code", "client_id": "spa", "redirect_uri": REDIRECT,
        "scope": "openid", "state": "s1",
        "code_challenge": compute_s256_challenge(VERIFIER), "code_challenge_method": S256,
    })


def test_no_session_authorize_bounces_to_login():
    c = _app()
    r = _authorize(c)
    # No webgate session yet -> the resolver returns None -> authorize redirects
    # to the webgate login (not the app), threading the authorize URL to return to.
    assert r.status_code == 302
    loc = urlparse(r.headers["location"])
    assert loc.path == "/gate/login"
    assert "return_to" in parse_qs(loc.query)


def test_one_webgate_login_satisfies_oauth_authorize():
    c = _app()
    # 1) log in at the webgate (sets the shared session cookie)
    assert c.post("/gate/login", data={"email": "alice@example.org", "password": PW,
                                       "next": "/"}).status_code == 303
    # 2) the SAME browser hits /oauth/authorize — no second login
    r = _authorize(c)
    assert r.status_code == 302
    loc = urlparse(r.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == REDIRECT
    q = parse_qs(loc.query)
    assert q["state"] == ["s1"]
    assert "code" in q  # authorization code issued to the logged-in user
