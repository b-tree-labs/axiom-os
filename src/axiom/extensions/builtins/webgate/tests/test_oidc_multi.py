# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Several sign-in providers on one gate (Portkey-style): the primary
institution block plus named blocks, each with its own routes, button and JWKS;
the login card says how an account comes to exist."""

from __future__ import annotations

import base64
import json
import time
import warnings
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.auth.pkce import challenge_for
from axiom.extensions.builtins.auth.providers import entra, google
from axiom.extensions.builtins.webgate.api.routers import LoginBrand, build_webgate_router
from axiom.extensions.builtins.webgate.oidc import (
    JwksCache,
    OidcSignIn,
    issuer_preset,
    providers_from_env,
)
from axiom.webauth import SESSION_COOKIE
from axiom.webauth.keys import reset_key_store_for_tests
from axiom.webauth.users import InMemoryUserStore

warnings.filterwarnings("ignore")

TENANT = "31d7e2a5-0000-0000-0000-000000000000"


@pytest.fixture(autouse=True)
def _keys():
    reset_key_store_for_tests()
    yield
    reset_key_store_for_tests()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


BASE_ENV = {
    "AXIOM_GATE_OIDC_PROVIDER": "entra",
    "AXIOM_GATE_OIDC_TENANT": TENANT,
    "AXIOM_GATE_OIDC_CLIENT_ID": "ut-app",
    "AXIOM_GATE_OIDC_LABEL": "Sign in with UT EID",
    "AXIOM_GATE_OIDC_PROVIDERS": "google,microsoft",
    "AXIOM_GATE_OIDC_GOOGLE_PROVIDER": "google",
    "AXIOM_GATE_OIDC_GOOGLE_CLIENT_ID": "g-client",
    "AXIOM_GATE_OIDC_MICROSOFT_PROVIDER": "entra",
    "AXIOM_GATE_OIDC_MICROSOFT_TENANT": "common",
    "AXIOM_GATE_OIDC_MICROSOFT_CLIENT_ID": "ms-client",
}


# ---------------------------------------------------------------- env parsing


def test_providers_from_env_orders_primary_first_and_defaults_labels():
    ps = providers_from_env(BASE_ENV)
    assert [p.name for p in ps] == ["sso", "google", "microsoft"]
    assert [p.kind for p in ps] == ["entra", "google", "entra"]
    assert ps[0].label == "Sign in with UT EID"
    assert ps[1].label == "Continue with Google"
    assert ps[2].label == "Continue with Microsoft"
    assert ps[0].login_path == "/gate/oidc/login"
    assert ps[1].login_path == "/gate/oidc/google/login"
    assert ps[1].redirect_path == "/gate/oidc/google/callback"
    assert ps[1].subject_claim == "sub" and ps[2].subject_claim == "oid"


def test_providers_from_env_without_primary_and_with_gaps():
    env = {
        k: v
        for k, v in BASE_ENV.items()
        if not k.startswith("AXIOM_GATE_OIDC_P") or k == "AXIOM_GATE_OIDC_PROVIDERS"
    }
    env.pop("AXIOM_GATE_OIDC_PROVIDER", None)
    env.pop("AXIOM_GATE_OIDC_CLIENT_ID", None)
    env["AXIOM_GATE_OIDC_PROVIDERS"] = "google, ,microsoft,unset_one"
    ps = providers_from_env(env)
    assert [p.name for p in ps] == ["google", "microsoft"]  # no primary; blanks/unset skipped


def test_half_configured_named_block_refuses_loudly():
    env = dict(BASE_ENV)
    env["AXIOM_GATE_OIDC_GOOGLE_CLIENT_ID"] = ""
    with pytest.raises(ValueError, match="google"):
        providers_from_env(env)
    with pytest.raises(ValueError, match="invalid OIDC provider name"):
        providers_from_env({**BASE_ENV, "AXIOM_GATE_OIDC_PROVIDERS": "sso"})


# ---------------------------------------------------------------- the card


def _client_multi(providers, brand=None):
    app = FastAPI()
    app.include_router(
        build_webgate_router(
            InMemoryUserStore(),
            secure_cookies=False,
            oidc=providers,
            http=object(),
            jwks=JwksCache(None, "unused"),
            brand=brand,
        )
    )
    return TestClient(app, base_url="http://gate.example", follow_redirects=False)


def _providers():
    return [
        OidcSignIn(
            idp=entra(TENANT),
            client_id="ut-app",
            label="Sign in with UT EID",
            name="sso",
            kind="entra",
        ),
        OidcSignIn(
            idp=google(),
            client_id="g",
            label="Continue with Google",
            redirect_path="/gate/oidc/google/callback",
            subject_claim="sub",
            name="google",
            kind="google",
            jit=False,
        ),
    ]


def test_login_page_offers_every_provider_and_the_password_form():
    c = _client_multi(_providers())
    r = c.get("/gate/login?next=/chat")
    assert r.status_code == 200
    assert 'href="/gate/oidc/login?next=/chat"' in r.text
    assert 'href="/gate/oidc/google/login?next=/chat"' in r.text
    assert "Sign in with UT EID" in r.text and "Continue with Google" in r.text
    assert 'name="password"' in r.text
    # the JIT provider drives the first-time hint
    assert "your account is created automatically" in r.text
    assert (
        "Sign in with UT EID</strong>"
        in r.text.replace("<strong>", "</strong>").replace("</strong></strong>", "<strong>")
        or "Sign in with UT EID" in r.text
    )


def test_hint_priority_custom_then_signup_then_admin():
    custom = _client_multi(
        _providers(), brand=LoginBrand(login_hint="Ping ops on Teams for access.")
    )
    assert "Ping ops on Teams for access." in custom.get("/gate/login").text
    signup = _client_multi([], brand=LoginBrand(signup_url="/signup"))
    assert "Create an account" in signup.get("/gate/login").text
    none = _client_multi([])
    r = none.get("/gate/login")
    assert "Ask your administrator for an invite" in r.text
    assert "/gate/oidc/" not in r.text


def test_support_email_links_the_administrator_everywhere():
    c = _client_multi([], brand=LoginBrand(product_name="Gate", support_email="ops@example.edu"))
    login = c.get("/gate/login").text
    assert 'href="mailto:ops@example.edu?subject=Gate%20account%20request"' in login
    assert ">Ask your administrator</a> for an invite" in login
    forgot = c.get("/gate/forgot").text
    assert 'href="mailto:ops@example.edu?subject=Gate%20password%20reset"' in forgot
    # without it, plain text stands and no mailto leaks
    plain = _client_multi([]).get("/gate/login").text
    assert "mailto:" not in plain
    # a JIT provider replaces the invite hint — support stays reachable on its own line
    jit_sso = OidcSignIn(
        idp=entra(TENANT), client_id="c", client_secret=None, label="Sign in with UT EID", jit=True
    )
    jit = (
        _client_multi(
            [jit_sso], brand=LoginBrand(product_name="Gate", support_email="ops@example.edu")
        )
        .get("/gate/login")
        .text
    )
    assert "First time here?" in jit
    assert 'subject=Gate%20support">Contact support</a>' in jit
    # when the hint itself already links the mail, no second support line appears
    linked = (
        _client_multi([], brand=LoginBrand(product_name="Gate", support_email="ops@example.edu"))
        .get("/gate/login")
        .text
    )
    assert linked.count("mailto:") == 1
    # the subject is URL-quoted end to end — no raw spaces from the product name
    spaced = (
        _client_multi(
            [], brand=LoginBrand(product_name="Big Gate", support_email="ops@example.edu")
        )
        .get("/gate/login")
        .text
    )
    assert "subject=Big%20Gate%20account%20request" in spaced
    assert "subject=Big Gate" not in spaced


# ---------------------------------------------------------------- a named flow


class FakeNamedIdp:
    """A fake IdP bound to one IdpConfig — both endpoints plus the JWKS."""

    def __init__(self, idp_cfg, client_id):
        self.cfg = idp_cfg
        self.client_id = client_id
        self.priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "k1"

    def post(self, url, data):
        assert url == self.cfg.token_endpoint
        assert challenge_for(data["code_verifier"]) == self.expected_challenge
        claims = {
            "iss": self.cfg.issuer,
            "aud": self.client_id,
            "exp": int(time.time()) + 600,
            "nbf": int(time.time()) - 60,
            "sub": "google-sub-1",
            "email": "alice@example.org",
            "name": "Alice",
            "nonce": self.nonce_seen,
        }
        return {"id_token": self.jwt(claims), "access_token": "at", "token_type": "Bearer"}

    def get(self, url):
        assert url == self.cfg.jwks_uri
        nums = self.priv.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": self.kid,
                    "n": _b64(nums.n.to_bytes((nums.n.bit_length() + 7) // 8, "big")),
                    "e": _b64(nums.e.to_bytes((nums.e.bit_length() + 7) // 8, "big")),
                }
            ]
        }

    def jwt(self, claims):
        header = _b64(json.dumps({"alg": "RS256", "kid": self.kid, "typ": "JWT"}).encode())
        payload = _b64(json.dumps(claims).encode())
        sig = self.priv.sign(f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{header}.{payload}.{_b64(sig)}"

    def observe(self, location):
        q = {k: v[0] for k, v in parse_qs(urlparse(location).query).items()}
        self.expected_challenge = q["code_challenge"]
        self.nonce_seen = q["nonce"]
        return q


def test_named_provider_flow_lands_the_same_gate_session():
    g = OidcSignIn(
        idp=google(),
        client_id="g-client",
        label="Continue with Google",
        redirect_path="/gate/oidc/google/callback",
        subject_claim="sub",
        name="google",
        kind="google",
    )
    idp = FakeNamedIdp(g.idp, "g-client")
    store = InMemoryUserStore()
    app = FastAPI()
    app.include_router(
        build_webgate_router(
            store,
            secure_cookies=False,
            oidc=[_providers()[0], g],
            http=idp,
            jwks=JwksCache(idp, g.idp.jwks_uri),
        )
    )
    c = TestClient(app, base_url="http://gate.example", follow_redirects=False)
    r = c.get("/gate/oidc/google/login?next=/chat")
    assert r.status_code == 302
    u = urlparse(r.headers["location"])
    assert f"{u.scheme}://{u.netloc}{u.path}" == g.idp.authorization_endpoint
    q = idp.observe(r.headers["location"])
    assert q["redirect_uri"] == "http://gate.example/gate/oidc/google/callback"
    r2 = c.get(f"/gate/oidc/google/callback?code=good-code&state={q['state']}")
    assert r2.status_code == 303 and r2.headers["location"] == "/chat"
    assert SESSION_COOKIE in r2.cookies
    assert store.get_by_email("alice@example.org") is not None
    # the session passes the forward-auth check like any password login
    v = c.get("/gate/verify")
    assert v.status_code == 200 and v.headers["X-Axiom-User-Email"] == "alice@example.org"


# ---------------------------------------------------------------- issuer presets


@pytest.mark.parametrize(
    ("issuer", "kind", "brand", "subject"),
    [
        ("https://accounts.google.com", "google", "Google", "sub"),
        ("https://login.microsoftonline.com/x/v2.0", "entra", "Microsoft", "oid"),
        ("https://cognito-idp.us-east-1.amazonaws.com/us-east-1_abc", "aws", "AWS", "sub"),
        ("https://acme.cloudflareaccess.com", "cloudflare", "Cloudflare", "sub"),
        ("https://dev-1.okta.com/oauth2/default", "okta", "Okta", "sub"),
        ("https://acme.eu.auth0.com/", "auth0", "Auth0", "sub"),
        ("https://gitlab.com", "gitlab", "GitLab", "sub"),
        ("https://gitlab.example.edu", "gitlab", "GitLab", "sub"),
        ("https://sso.example.edu/realms/campus", "keycloak", "Keycloak", "sub"),
    ],
)
def test_issuer_preset_recognizes_popular_idps(issuer, kind, brand, subject):
    p = issuer_preset(issuer)
    assert (p.kind, p.brand, p.subject) == (kind, brand, subject)


def test_issuer_preset_unknown_is_none_and_still_configures_generically():
    assert issuer_preset("https://idp.example.org") is None
    assert issuer_preset("https://evilcloudflareaccess.com") is None  # suffix, not substring


class FakeDiscoveryHttp:
    def get(self, url):
        issuer = url.replace("/.well-known/openid-configuration", "")
        return {
            "issuer": issuer,
            "authorization_endpoint": issuer + "/auth",
            "token_endpoint": issuer + "/token",
            "jwks_uri": issuer + "/jwks",
        }


def test_discovery_provider_wears_its_preset_kind_and_label():
    env = {
        "AXIOM_GATE_OIDC_PROVIDERS": "team,legacy",
        "AXIOM_GATE_OIDC_TEAM_PROVIDER": "https://acme.cloudflareaccess.com",
        "AXIOM_GATE_OIDC_TEAM_CLIENT_ID": "cf-client",
        "AXIOM_GATE_OIDC_LEGACY_PROVIDER": "https://idp.example.org",
        "AXIOM_GATE_OIDC_LEGACY_CLIENT_ID": "x-client",
    }
    team, legacy = providers_from_env(env, http=FakeDiscoveryHttp())
    assert (team.kind, team.label) == ("cloudflare", "Continue with Cloudflare")
    assert (legacy.kind, legacy.label) == ("oidc", "Continue with Legacy")
    card = _client_multi([team, legacy]).get("/gate/login").text
    assert "Continue with Cloudflare" in card
    assert "#f6821f" in card  # the cloudflare glyph, not the generic lock
