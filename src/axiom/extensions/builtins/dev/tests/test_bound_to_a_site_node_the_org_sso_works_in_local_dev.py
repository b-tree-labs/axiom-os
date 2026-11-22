# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Acceptance (#1717): "If bound to a Site node, the org OIDC/SSO should work for real
when running in local dev mode."

Bound, and the site publishes a development client: `dev up` with no flag and no
vault entry offers the site's SSO by default, the sign-in completes (PKCE, no
secret), and the person is the same principal locally as on the site.
Unbound: password sign-in, no SSO. The site node and the identity provider are
fakes; the node's configuration, the gate and the accounts file are real.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.dev import signin
from axiom.extensions.builtins.dev.skills import up
from axiom.extensions.builtins.webgate.api.routers import build_webgate_router
from axiom.extensions.builtins.webgate.oidc import (
    JwksCache,
    OidcSignIn,
    providers_from_env,
    resolve_account,
)
from axiom.extensions.builtins.webgate.tests.test_a_public_client_signs_in_with_pkce_and_no_secret import (  # noqa: E501
    CLIENT,
    TENANT,
    FakeIdp,
)
from axiom.webauth import SESSION_COOKIE, JsonFileUserStore
from axiom.webauth.users import InMemoryUserStore

REAL_FROM_BOUND_SITE = signin.from_bound_site
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"
BOUND = SimpleNamespace(bound=True, face_url="https://node.example.org", site="example-site")
EMAIL = "person@example.org"


def _site_publishes(status=200):
    body = {"issuer_url": ISSUER, "client_id": CLIENT, "client_type": "public",
            "allowed_redirects": ["http://localhost/gate/oidc/callback"]}
    return lambda url, headers, timeout: (status, body if status == 200 else {})


def _up(harness, **params):
    result = up.run({"email": EMAIL, "db_url": "postgresql://u@127.0.0.1:5432/dev", **params},
                    harness["ctx"])
    assert result.ok, result.errors
    return result, harness["started"][-1]


def _gate(plan, idp=None):
    """The gate the dev node would serve, built from the node's own environment."""
    env = {k: v for k, v in plan.env.items() if k.startswith("AXIOM_GATE_")}
    providers = providers_from_env(env)
    store = JsonFileUserStore(plan.env["AXIOM_GATE_USERS_FILE"])
    app = FastAPI()
    app.include_router(
        build_webgate_router(
            store,
            secure_cookies=False,
            oidc=providers,
            http=idp or object(),
            jwks=JwksCache(idp, providers[0].idp.jwks_uri) if providers else None,
        )
    )
    client = TestClient(app, base_url=f"http://127.0.0.1:{plan.port}", follow_redirects=False)
    return client, providers, store


def test_bound_and_published_sso_is_the_default_and_completes(harness, monkeypatch):
    monkeypatch.setattr(
        signin, "from_bound_site",
        lambda **kw: REAL_FROM_BOUND_SITE(mode=BOUND, http_get=_site_publishes(), key="k"),
    )
    # No flag, no vault entry: just `dev up`.
    result, plan = _up(harness)
    assert result.value["sign_in_public"] and result.value["sign_in_source"] == "bound site"

    idp = FakeIdp(claims_extra={
        "oid": "subject-42", "email": EMAIL, "preferred_username": "id42@eid.example.edu",
    })
    client, providers, store = _gate(plan, idp)
    (sso,) = providers
    assert sso.public and sso.client_secret is None

    # The sign-in page offers the site's SSO; the password form stays as the fallback.
    page = client.get("/gate/login").text
    assert "/gate/oidc/login" in page and 'name="password"' in page

    r = client.get("/gate/oidc/login?next=/")
    q = idp.observe(r.headers["location"])
    assert q["code_challenge_method"] == "S256"
    assert q["redirect_uri"] == f"http://127.0.0.1:{plan.port}/gate/oidc/callback"
    done = client.get(f"/gate/oidc/callback?code=c&state={q['state']}")
    assert done.status_code == 303 and SESSION_COOKIE in done.cookies
    assert all("client_secret" not in sent for sent in idp.posts)

    # The same principal the site derives from the same token, not a local one:
    # the password account `dev up` made was re-keyed to the site's subject.
    local = store.get_by_email(EMAIL)
    site_cfg = OidcSignIn(idp=sso.idp, client_id=CLIENT, client_secret="site-only")
    site = resolve_account(site_cfg, InMemoryUserStore(), {
        "oid": "subject-42", "email": EMAIL, "preferred_username": "id42@eid.example.edu",
    })
    assert local.user_id == site.user_id == "subject-42"
    assert local.handle == site.handle
    assert local.password_hash  # password still works as the fallback
    assert len(store) == 1


def test_unbound_signs_in_with_a_password(harness):
    # The harness's install is not bound: nobody is asked, no SSO is configured.
    result, plan = _up(harness)
    assert result.value["sign_in"] is None
    client, providers, _ = _gate(plan)
    assert providers == []
    page = client.get("/gate/login").text
    assert 'name="password"' in page and "/gate/oidc/login" not in page


def test_bound_but_not_published_falls_back_to_password_and_says_why(harness, monkeypatch):
    monkeypatch.setattr(
        signin, "from_bound_site",
        lambda **kw: REAL_FROM_BOUND_SITE(mode=BOUND, http_get=_site_publishes(404), key="k"),
    )
    result, plan = _up(harness)
    assert result.value["sign_in"] is None
    assert signin.NOT_ENABLED in result.value["notes"]
    _, providers, _ = _gate(plan)
    assert providers == []
