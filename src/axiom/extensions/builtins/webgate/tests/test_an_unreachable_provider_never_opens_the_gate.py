# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A sign-in provider that cannot be reached never takes the gate down (#1842).

The gate read the issuer's discovery document while it was being built. When
the issuer did not answer, building the gate raised, the app composer dropped
the gate's mount as a broken one, and the node served its pages with no
sign-in at all. The gate now stands, and the provider refuses to start a
sign-in until its issuer answers.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.webgate import oidc
from axiom.extensions.builtins.webgate.api.routers import build_webgate_router
from axiom.extensions.builtins.webgate.oidc import (
    UNREACHABLE,
    OidcSignIn,
    OidcSignInError,
    begin,
    rediscover,
)
from axiom.webauth.keys import reset_key_store_for_tests
from axiom.webauth.users import InMemoryUserStore

ISSUER = "https://idp.example.test"
ENV = {
    "AXIOM_GATE_OIDC_PROVIDER": ISSUER,
    "AXIOM_GATE_OIDC_CLIENT_ID": "client-1",
    "AXIOM_GATE_OIDC_CLIENT_TYPE": "public",
}
DOCUMENT = {
    "issuer": ISSUER,
    "authorization_endpoint": ISSUER + "/authorize",
    "token_endpoint": ISSUER + "/token",
    "jwks_uri": ISSUER + "/keys",
}


class Http:
    """An issuer that is down until ``up`` is set."""

    def __init__(self, up: bool = False) -> None:
        self.up = up

    def get(self, url: str) -> dict:
        if not self.up:
            raise OSError("connection refused")
        return dict(DOCUMENT)


@pytest.fixture(autouse=True)
def _keys():
    reset_key_store_for_tests()
    yield
    reset_key_store_for_tests()


def _client(http: Http) -> TestClient:
    provider = OidcSignIn.from_env(ENV, http=http)
    app = FastAPI()
    app.include_router(
        build_webgate_router(
            oidc=provider, http=http, user_store=InMemoryUserStore(), secure_cookies=False
        )
    )
    return TestClient(app, base_url="http://127.0.0.1:8123", follow_redirects=False)


def test_an_unreachable_issuer_still_yields_a_provider_and_it_is_marked():
    provider = OidcSignIn.from_env(ENV, http=Http())
    assert provider is not None and provider.unreachable


def test_a_reachable_issuer_is_not_marked():
    """Negative control: the mark is not simply always set."""
    provider = OidcSignIn.from_env(ENV, http=Http(up=True))
    assert provider is not None and not provider.unreachable
    assert provider.idp.authorization_endpoint == ISSUER + "/authorize"


def test_an_unreachable_provider_starts_no_sign_in():
    provider = OidcSignIn.from_env(ENV, http=Http())
    with pytest.raises(OidcSignInError) as refused:
        begin(provider, base_url="http://127.0.0.1:1/", next_target="/", remember=False, issuer="x")
    assert refused.value.public == UNREACHABLE


def test_the_gate_stands_and_says_the_provider_cannot_be_reached():
    client = _client(Http())
    assert client.get("/gate/login").status_code == 200
    answer = client.get("/gate/oidc/login")
    assert answer.status_code == 401, "nobody is redirected to an endpoint that was never read"
    assert UNREACHABLE in answer.text


def test_sign_in_starts_once_the_issuer_answers():
    http = Http()
    client = _client(http)
    assert UNREACHABLE in client.get("/gate/oidc/login").text
    http.up = True
    answer = client.get("/gate/oidc/login")
    assert answer.status_code == 302
    assert answer.headers["location"].startswith(ISSUER + "/authorize")


def test_rediscover_refuses_while_the_issuer_is_still_down():
    provider = OidcSignIn.from_env(ENV, http=Http())
    with pytest.raises(OidcSignInError):
        rediscover(provider, Http())


def test_the_gate_mount_is_built_when_the_issuer_is_down(monkeypatch):
    """The hole itself: the mount the app composer asks for must exist."""
    from axiom.extensions.builtins.webgate.mount import mount_spec

    def down(self, url):
        raise OSError("connection refused")

    monkeypatch.setattr(oidc.UrllibHttp, "get", down)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    spec = mount_spec()
    assert spec.prefix == "/gate"
    assert any(getattr(r, "path", "") == "/gate/login" for r in spec.router.routes)
