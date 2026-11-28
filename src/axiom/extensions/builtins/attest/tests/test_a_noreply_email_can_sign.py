# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A GitHub ``ID+name@users.noreply.github.com`` sign-in can use the Log (#1789).

The contributor quickstart tells people to use that address. `dev up` signed
them in with it and ``/gate/me`` answered, but every attest route said
"sign in first": ``1234+name`` is not an ADR-020 name, the handle could not
be made, and the API reported the failure as no session at all.
"""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from axiom.extensions.builtins.attest import api  # noqa: E402
from axiom.extensions.builtins.fleet import api as fleet_api  # noqa: E402
from axiom.extensions.builtins.http.authz_hook import build_session_resolver  # noqa: E402
from axiom.extensions.builtins.webgate.api.routers import build_webgate_router  # noqa: E402
from axiom.webauth import SESSION_COOKIE  # noqa: E402
from axiom.webauth.keys import reset_key_store_for_tests  # noqa: E402
from axiom.webauth.session import issue_session_token  # noqa: E402
from axiom.webauth.users import InMemoryUserStore, User  # noqa: E402

from ..naming import SignerNameError, signer_handle  # noqa: E402

BASE = "http://gate.example"
NOREPLY = "12345+octo-cat@users.noreply.github.com"


def _user(sub: str) -> User:
    return User(user_id=sub, email=sub, name="", roles=("owner",), site="site-a")


@pytest.fixture(autouse=True)
def no_site_env(monkeypatch):
    monkeypatch.delenv("AXIOM_SITE", raising=False)
    monkeypatch.delenv("AXIOM_SERVED_SITES", raising=False)


@pytest.fixture
def client():
    """Only the door: the routes below answer before any store is opened."""
    reset_key_store_for_tests()
    fleet_api.set_session_resolver_factory(lambda: build_session_resolver(issuer=BASE))
    app = fastapi.FastAPI()
    router = fastapi.APIRouter(prefix="/api/v1")
    api.register_routes(router)
    app.include_router(router)
    app.include_router(build_webgate_router(InMemoryUserStore(), secure_cookies=False))
    yield TestClient(app, base_url=BASE, follow_redirects=False)
    fleet_api.reset_session_resolver_factory()
    reset_key_store_for_tests()


def as_(c, sub: str):
    c.cookies.set(SESSION_COOKIE, issue_session_token(_user(sub), issuer=BASE, amr=("pwd",)))
    return c


def test_a_noreply_email_is_named_by_its_github_login():
    assert signer_handle({"sub": NOREPLY, "site": "site-a"}) == "@octo-cat:site-a"


def test_an_ordinary_email_is_named_as_before():
    assert signer_handle({"sub": "op1@example.org", "site": "site-a"}) == "@op1:site-a"


def test_a_plus_address_elsewhere_is_sanitised_to_a_valid_name():
    assert signer_handle({"sub": "first+tag@example.org", "site": "s"}) == "@first-tag:s"


def test_an_email_with_nothing_usable_says_what_is_wrong_with_it():
    with pytest.raises(SignerNameError) as raised:
        signer_handle({"sub": "+++@example.org"})
    assert "+++@example.org" in str(raised.value)


def test_the_noreply_person_reaches_the_log(client):
    r = as_(client, NOREPLY).get("/api/v1/attest/logbooks")
    assert r.status_code == 200, r.text


def test_an_unnamable_email_is_told_so_not_sent_to_sign_in(client):
    r = as_(client, "a@b@example.org").get("/api/v1/attest/logbooks")
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["code"] == "signer_name"
    assert "a@b@example.org" in body["detail"]
    assert "sign in first" not in body["detail"]


def test_the_gate_refuses_a_grant_to_an_unnamable_email_the_same_way(client):
    r = as_(client, "a@b@example.org").post("/gate/grants", json={"presentation_id": "x"})
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "signer_name"


def test_a_session_still_cannot_propose_even_when_unnamable(client):
    r = as_(client, "a@b@example.org").post("/api/v1/attest/proposals", json={})
    assert r.status_code == 403
    assert r.json()["code"] == "session"


def test_no_session_is_still_no_session(client):
    r = client.get("/api/v1/attest/logbooks")
    assert r.status_code == 401 and r.json()["code"] == "no_session"
