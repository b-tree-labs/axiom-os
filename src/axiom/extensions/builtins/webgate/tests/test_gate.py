# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The webgate forward-auth gate: login -> session cookie -> /verify (ADR-003).

An edge proxy sits in front of any UI and subrequests ``/gate/verify``: 200 +
identity headers = allow (headers forwarded to the UI/shim), 401 = deny (proxy
sends the browser to ``/gate/login``). These tests drive that contract.
"""

from __future__ import annotations

import warnings

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.webgate.api.routers import build_webgate_router
from axiom.webauth import SESSION_COOKIE, get_password_hash
from axiom.webauth.keys import reset_key_store_for_tests
from axiom.webauth.users import InMemoryUserStore, User

warnings.filterwarnings("ignore")

PW = "correct-horse-battery-staple"


@pytest.fixture(autouse=True)
def _keys():
    reset_key_store_for_tests()
    yield
    reset_key_store_for_tests()


def _store():
    return InMemoryUserStore([
        User(user_id="u1", email="alice@example.org",
             password_hash=get_password_hash(PW), name="Alice", roles=("user", "op")),
    ])


def _client(store=None):
    app = FastAPI()
    # secure_cookies=False so the cookie is set over the TestClient's http.
    app.include_router(build_webgate_router(store or _store(), secure_cookies=False))
    return TestClient(app, base_url="http://gate.example", follow_redirects=False)


def _login(c, email="alice@example.org", password=PW, next_="/chat"):
    return c.post("/gate/login", data={"email": email, "password": password, "next": next_})


def test_login_page_renders():
    r = _client().get("/gate/login")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert 'name="password"' in r.text


def test_successful_login_sets_cookie_and_redirects():
    c = _client()
    r = _login(c)
    assert r.status_code == 303
    assert r.headers["location"] == "/chat"
    assert SESSION_COOKIE in r.cookies


def test_verify_allows_with_session_and_emits_identity_headers():
    c = _client()
    _login(c)  # TestClient now holds the session cookie
    r = c.get("/gate/verify")
    assert r.status_code == 200
    assert r.headers["X-Axiom-User-Email"] == "alice@example.org"
    assert r.headers["X-Axiom-User-Id"] == "u1"
    assert r.headers["X-Axiom-User-Name"] == "Alice"
    assert "user" in r.headers["X-Axiom-User-Roles"]


def test_verify_denies_without_session():
    r = _client().get("/gate/verify")
    assert r.status_code == 401


def test_wrong_password_denied_no_cookie():
    c = _client()
    r = _login(c, password="nope")
    assert r.status_code == 401
    assert SESSION_COOKIE not in r.cookies


def test_unknown_user_denied():
    c = _client()
    r = _login(c, email="ghost@example.org")
    assert r.status_code == 401


def test_logout_clears_session():
    c = _client()
    _login(c)
    assert c.get("/gate/verify").status_code == 200
    out = c.post("/gate/logout")
    assert out.status_code == 303
    assert out.headers["location"] == "/gate/login"
    # cookie cleared -> verify now denies
    assert c.get("/gate/verify").status_code == 401


def test_logout_via_get_clears_session():
    # A browser navigation (GET) must log out too -- this is the path a consumer
    # UI's post-signout redirect (e.g. Open WebUI's WEBUI_AUTH_SIGNOUT_REDIRECT_URL)
    # takes to end the gate session, so a trusted-header UI isn't re-authed back in.
    c = _client()
    _login(c)
    assert c.get("/gate/verify").status_code == 200
    out = c.get("/gate/logout")
    assert out.status_code == 303
    assert out.headers["location"] == "/gate/login"
    assert c.get("/gate/verify").status_code == 401


def test_open_redirect_next_is_sanitized_to_local_path():
    c = _client()
    r = _login(c, next_="https://evil.example/pwn")
    assert r.status_code == 303
    assert r.headers["location"] == "/"  # external target rejected


def test_protocol_relative_next_rejected():
    c = _client()
    r = _login(c, next_="//evil.example")
    assert r.headers["location"] == "/"


def test_forwarded_uri_becomes_login_next_hint():
    # A proxy passes the originally-requested path; the login page threads it back.
    r = _client().get("/gate/login", params={"next": "/chat/room/7"})
    assert 'value="/chat/room/7"' in r.text


def test_forgot_page_renders():
    r = _client().get("/gate/forgot")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_remember_me_extends_cookie_lifetime():
    import re

    def _max_age(resp):
        return int(re.search(r"[Mm]ax-[Aa]ge=(\d+)", resp.headers["set-cookie"]).group(1))

    default = _login(_client())  # no "remember"
    remembered = _client().post(
        "/gate/login",
        data={"email": "alice@example.org", "password": PW, "next": "/", "remember": "1"},
    )
    assert _max_age(remembered) > _max_age(default)
    assert _max_age(remembered) >= 29 * 24 * 3600  # ~30-day remember window


def test_login_brand_accent_is_sanitized():
    from axiom.extensions.builtins.webgate.api.routers import LoginBrand

    assert LoginBrand(accent="#BF5700").safe_accent() == "#BF5700"
    assert LoginBrand(accent="rebeccapurple").safe_accent() == "rebeccapurple"
    # a CSS-injection attempt is rejected -> the brand default, never raw into <style>
    assert LoginBrand(accent="#fff;}body{display:none").safe_accent() == "#bf5700"


def test_json_session_login_sets_cookie_and_returns_user():
    c = _client()
    r = c.post("/gate/session", json={"email": "alice@example.org", "password": PW})
    assert r.status_code == 200
    assert SESSION_COOKIE in r.cookies
    body = r.json()
    assert body["email"] == "alice@example.org"
    assert body["role"] == "user"  # first role — SoilMetrix-compat singular
    assert "user" in body["roles"]


def test_json_session_login_bad_creds_returns_401_detail():
    r = _client().post("/gate/session", json={"email": "alice@example.org", "password": "nope"})
    assert r.status_code == 401
    assert r.json()["detail"] == "Incorrect email or password"
    assert SESSION_COOKIE not in r.cookies


def test_gate_me_reflects_session():
    c = _client()
    assert c.get("/gate/me").status_code == 401  # no session yet
    c.post("/gate/session", json={"email": "alice@example.org", "password": PW})
    me = c.get("/gate/me")
    assert me.status_code == 200
    assert me.json()["email"] == "alice@example.org"


def test_json_session_remember_extends_cookie():
    import re

    def _ma(r):
        return int(re.search(r"[Mm]ax-[Aa]ge=(\d+)", r.headers["set-cookie"]).group(1))

    short = _client().post("/gate/session", json={"email": "alice@example.org", "password": PW})
    long = _client().post(
        "/gate/session", json={"email": "alice@example.org", "password": PW, "remember": True}
    )
    assert _ma(long) > _ma(short)


def test_brand_skins_the_default_login_ui():
    from axiom.extensions.builtins.webgate.api.routers import LoginBrand

    app = FastAPI()
    app.include_router(
        build_webgate_router(
            _store(), secure_cookies=False,
            brand=LoginBrand(product_name="Field Hand", accent="#2e7d32"),
        )
    )
    r = TestClient(app, base_url="http://gate.example").get("/gate/login")
    assert "Field Hand" in r.text  # product name rendered
    assert "#2e7d32" in r.text  # brandable accent injected
