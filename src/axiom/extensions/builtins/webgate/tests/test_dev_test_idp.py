# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A dev lane's test identity provider: a real OIDC sign-in, on a dev lane only (DT-A1 to DT-A5).

Some acts are signed only with the organisation's sign-in (the approvals log asks posture ``sso``),
so a dev lane whose personas sign in by password cannot exercise them. The lane's own node serves a
test identity provider under ``/dev-idp/<lane>/``: discovery, authorize (a sign-in form for the
lane's own accounts), token (authorization code + PKCE) and JWKS. The gate reaches it through the
provider kind ``dev-test``. It works only on a dev lane: the lane must be in the lane registry, the
provider's address must be loopback at one of that lane's ports, and every request must come from
loopback; anywhere else the gate stays up, the provider refuses sign-in, and no route is mounted.
Its issuer is named ``dev-test:<lane>``, so every session and every record shows it was a test
sign-in.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.webgate.api import dev_test_idp
from axiom.extensions.builtins.webgate.api.routers import build_webgate_router
from axiom.extensions.builtins.webgate.oidc import providers_from_env
from axiom.webauth import SESSION_COOKIE
from axiom.webauth.keys import reset_key_store_for_tests
from axiom.webauth.session import verify_session_token
from axiom.webauth.password import get_password_hash
from axiom.webauth.users import InMemoryUserStore, User

warnings.filterwarnings("ignore")

LANE = "lane-a"
BASE = "http://127.0.0.1:9101"
EMAIL = "manager.example@dev.localhost"


@pytest.fixture(autouse=True)
def _keys():
    reset_key_store_for_tests()
    dev_test_idp.reset_for_tests()
    yield
    reset_key_store_for_tests()
    dev_test_idp.reset_for_tests()


@pytest.fixture
def lanes(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "lanes.json"
    path.write_text(json.dumps({"lanes": {LANE: {"front": 9100, "api": 9101}}}), encoding="utf-8")
    monkeypatch.setenv("AXIOM_LANES_FILE", str(path))
    return path


def _env(**over) -> dict[str, str]:
    env = {
        "AXIOM_LANES_FILE": __import__("os").environ.get("AXIOM_LANES_FILE", ""),
        dev_test_idp.LANE_VAR: LANE,
        dev_test_idp.URL_VAR: BASE,
        "AXIOM_GATE_OIDC_PROVIDERS": "devtest",
        "AXIOM_GATE_OIDC_DEVTEST_PROVIDER": "dev-test",
        "AXIOM_GATE_OIDC_DEVTEST_CLIENT_ID": dev_test_idp.CLIENT_ID,
        "AXIOM_GATE_OIDC_DEVTEST_CLIENT_TYPE": "public",
    }
    env.update(over)
    return {k: v for k, v in env.items() if v is not None}


def _store() -> InMemoryUserStore:
    store = InMemoryUserStore()
    store.add(User(user_id="u-manager", email=EMAIL, name="Sample manager", roles=("admin",),
                   password_hash=get_password_hash("lane-password"), site="example-site"))  # fmt: skip
    return store


class _Loopback:
    """The gate's HTTP seam, answered by the same app over loopback, as on a lane."""

    def __init__(self) -> None:
        self.client: TestClient | None = None

    def post(self, url: str, data):
        r = self.client.post(urlparse(url).path, data=dict(data))
        if r.status_code >= 400:
            from axiom.extensions.builtins.webgate.oidc import IdpHttpError

            body = r.json()
            raise IdpHttpError(r.status_code, body.get("error", ""), body.get("error_description", ""))
        return r.json()

    def get(self, url: str):
        return self.client.get(urlparse(url).path).json()


def _app(env, monkeypatch, store=None):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    http = _Loopback()
    providers = providers_from_env(env, http=http)
    app = FastAPI()
    app.include_router(build_webgate_router(store or _store(), secure_cookies=False, oidc=providers, http=http))
    client = TestClient(app, base_url=BASE, follow_redirects=False, client=("127.0.0.1", 50123))
    http.client = client
    return client, providers


@pytest.mark.acceptance("DT-A1")
def test_dt_a1_the_guard_names_a_lane_only_on_a_registered_lane_at_its_own_loopback_port(lanes):
    assert dev_test_idp.lane_for(_env()) == LANE
    assert dev_test_idp.lane_for(_env(**{dev_test_idp.LANE_VAR: None})) is None
    assert dev_test_idp.lane_for(_env(**{dev_test_idp.LANE_VAR: "not-a-lane"})) is None
    assert dev_test_idp.lane_for(_env(**{dev_test_idp.URL_VAR: "http://10.0.0.5:9101"})) is None
    assert dev_test_idp.lane_for(_env(**{dev_test_idp.URL_VAR: "https://node.example:9101"})) is None
    assert dev_test_idp.lane_for(_env(**{dev_test_idp.URL_VAR: "http://127.0.0.1:9999"})) is None
    lanes.write_text(json.dumps({"lanes": {}}), encoding="utf-8")
    assert dev_test_idp.lane_for(_env()) is None


@pytest.mark.acceptance("DT-A2")
def test_dt_a2_outside_a_lane_the_gate_stays_up_the_provider_refuses_and_no_route_is_mounted(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_LANES_FILE", str(tmp_path / "missing.json"))
    c, providers = _app(_env(**{"AXIOM_LANES_FILE": str(tmp_path / "missing.json")}), monkeypatch)
    assert [p.name for p in providers] == ["devtest"]
    assert providers[0].unreachable
    assert c.get("/gate/login").status_code == 200
    assert c.get(f"/dev-idp/{LANE}/.well-known/openid-configuration").status_code == 404
    login = c.get(providers[0].login_path)
    assert login.status_code != 303 or "/dev-idp/" not in login.headers.get("location", "")


@pytest.mark.acceptance("DT-A3")
def test_dt_a3_on_a_lane_a_person_signs_in_through_a_real_oidc_flow_and_the_session_names_the_test_issuer(lanes, monkeypatch):
    c, providers = _app(_env(), monkeypatch)
    p = providers[0]
    assert not p.unreachable and p.idp.name == f"dev-test:{LANE}"
    start = c.get(f"{p.login_path}?next=/log")
    assert start.status_code in (302, 303)
    auth = urlparse(start.headers["location"])
    assert auth.path == f"/dev-idp/{LANE}/authorize"
    q = {k: v[0] for k, v in parse_qs(auth.query).items()}
    assert q["code_challenge_method"] == "S256" and q["client_id"] == dev_test_idp.CLIENT_ID
    form = c.get(f"{auth.path}?{auth.query}")
    assert form.status_code == 200 and "test sign-in" in form.text.lower()
    back = c.post(auth.path, data={**q, "email": EMAIL, "password": "lane-password"})
    assert back.status_code == 303
    cb = urlparse(back.headers["location"])
    got = {k: v[0] for k, v in parse_qs(cb.query).items()}
    assert got["state"] == q["state"] and got["code"]
    done = c.get(f"{cb.path}?{cb.query}")
    assert done.status_code in (302, 303), done.text
    claims = verify_session_token(done.cookies.get(SESSION_COOKIE) or c.cookies.get(SESSION_COOKIE), issuer=BASE)
    assert claims["idp"] == f"dev-test:{LANE}"
    assert claims["email"] == EMAIL


@pytest.mark.acceptance("DT-A4")
def test_dt_a4_the_flow_refuses_a_wrong_password_a_reused_code_and_a_wrong_verifier(lanes, monkeypatch):
    c, providers = _app(_env(), monkeypatch)
    start = c.get(providers[0].login_path)
    auth = urlparse(start.headers["location"])
    q = {k: v[0] for k, v in parse_qs(auth.query).items()}
    bad = c.post(auth.path, data={**q, "email": EMAIL, "password": "wrong"})
    assert bad.status_code == 401 and "code=" not in bad.headers.get("location", "")
    ok = c.post(auth.path, data={**q, "email": EMAIL, "password": "lane-password"})
    code = parse_qs(urlparse(ok.headers["location"]).query)["code"][0]
    token = f"/dev-idp/{LANE}/token"
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": q["redirect_uri"],
            "client_id": dev_test_idp.CLIENT_ID, "code_verifier": "not-the-verifier"}  # fmt: skip
    assert c.post(token, data=form).status_code == 400
    # A code is spent by its first use, right or wrong.
    assert c.post(token, data=form).status_code == 400


@pytest.mark.acceptance("DT-A5")
def test_dt_a5_on_a_lane_a_caller_that_is_not_loopback_is_refused(lanes, monkeypatch):
    for k, v in _env().items():
        monkeypatch.setenv(k, v)
    app = FastAPI()
    app.include_router(build_webgate_router(_store(), secure_cookies=False, oidc=providers_from_env(_env(), http=_Loopback())))
    far = TestClient(app, base_url=BASE, follow_redirects=False, client=("10.1.2.3", 50123))
    assert far.get(f"/dev-idp/{LANE}/.well-known/openid-configuration").status_code == 403
    assert far.get(f"/dev-idp/{LANE}/jwks").status_code == 403


@pytest.mark.acceptance("DT-A6")
def test_dt_a6_dev_up_sign_in_test_names_the_lane_its_own_address_and_a_public_client_beside_password():
    from axiom.extensions.builtins.dev import signin

    env = signin.test_environment(LANE, 9101)
    assert env[dev_test_idp.LANE_VAR] == LANE and env[dev_test_idp.URL_VAR] == BASE
    assert env["AXIOM_GATE_OIDC_PROVIDERS"] == "devtest"
    assert env["AXIOM_GATE_OIDC_DEVTEST_PROVIDER"] == "dev-test"
    assert env["AXIOM_GATE_OIDC_DEVTEST_CLIENT_TYPE"] == "public"
    assert "AXIOM_GATE_OIDC_PROVIDER" not in env  # the primary block is untouched: password stays
    assert not any("SECRET" in k for k in env)


@pytest.mark.acceptance("DT-A7")
def test_dt_a7_a_lane_tenant_host_is_loopback_for_the_test_provider_only(lanes, monkeypatch):
    from axiom.extensions.builtins.webgate.oidc import OidcSignInError

    _, providers = _app(_env(), monkeypatch)
    dev = providers[0]
    # A lane serves each tenant at <tenant>.localhost (RFC 6761: loopback), so its gate's own
    # redirect is one; the test provider takes it.
    assert dev.redirect_uri("http://example-site.localhost:9100") == "http://example-site.localhost:9100/gate/oidc/devtest/callback"
    with pytest.raises(OidcSignInError):
        dev.redirect_uri("http://node.example:9100")
    # A real provider's public client still answers the two forms such platforms register only.
    from dataclasses import replace

    real = replace(dev, kind="oidc")
    with pytest.raises(OidcSignInError):
        real.redirect_uri("http://example-site.localhost:9100")


@pytest.mark.acceptance("DT-A8")
def test_dt_a8_the_callback_does_not_block_the_event_loop_while_it_asks_the_provider(lanes, monkeypatch):
    """The callback's calls to the provider (token, keys) are blocking HTTP. Run on the event loop
    they stall every request the node serves, and a provider on this same node (a lane's test
    sign-in) can never answer: the sign-in times out. They run in a worker thread."""
    import asyncio
    import threading

    from axiom.extensions.builtins.webgate.api import routers

    seen: list[bool] = []
    real = routers.oidc_complete

    def spy(*a, **k):
        try:
            asyncio.get_running_loop()
            seen.append(False)  # called on the loop's thread
        except RuntimeError:
            seen.append(threading.current_thread() is not threading.main_thread() or True)
        return real(*a, **k)

    monkeypatch.setattr(routers, "oidc_complete", spy)
    c, providers = _app(_env(), monkeypatch)
    start = c.get(providers[0].login_path)
    auth = urlparse(start.headers["location"])
    q = {k: v[0] for k, v in parse_qs(auth.query).items()}
    back = c.post(auth.path, data={**q, "email": EMAIL, "password": "lane-password"})
    cb = urlparse(back.headers["location"])
    assert c.get(f"{cb.path}?{cb.query}").status_code in (302, 303)
    assert seen == [True]
