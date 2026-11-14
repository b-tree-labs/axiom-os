# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A public client signs in with PKCE alone: no secret is read, none is sent (#1717).

A contributor's local node signs in with the site's identity provider through a
public client: authorization code + PKCE, the loopback redirect the provider
registered for a public-client platform, and no client secret anywhere on the
laptop. Every test here runs against a fake provider.
"""

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
from axiom.extensions.builtins.auth.providers import entra
from axiom.extensions.builtins.webgate import oidc
from axiom.extensions.builtins.webgate.api.routers import build_webgate_router
from axiom.extensions.builtins.webgate.oidc import (
    IdpHttpError,
    JwksCache,
    OidcSignIn,
    OidcSignInError,
    explain_idp_error,
    resolve_account,
)
from axiom.webauth import SESSION_COOKIE
from axiom.webauth.keys import reset_key_store_for_tests
from axiom.webauth.users import InMemoryUserStore

warnings.filterwarnings("ignore")

TENANT = "00000000-0000-0000-0000-0000000000aa"
CLIENT = "00000000-0000-0000-0000-0000000000bb"


@pytest.fixture(autouse=True)
def _keys():
    reset_key_store_for_tests()
    yield
    reset_key_store_for_tests()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


PUBLIC_ENV = {
    "AXIOM_GATE_OIDC_PROVIDER": "entra",
    "AXIOM_GATE_OIDC_TENANT": TENANT,
    "AXIOM_GATE_OIDC_CLIENT_ID": CLIENT,
    "AXIOM_GATE_OIDC_CLIENT_TYPE": "public",
    "AXIOM_GATE_OIDC_IDP_ROLES": "0",
}


def _public(**kw) -> OidcSignIn:
    return OidcSignIn(idp=entra(TENANT), client_id=CLIENT, public=True, kind="entra", **kw)


class FakeIdp:
    """Token endpoint + JWKS. Records every token request it is sent."""

    def __init__(self, claims_extra=None, fail=None):
        self.cfg = entra(TENANT)
        self.priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.posts: list[dict] = []
        self.claims_extra = claims_extra or {}
        self.fail = fail

    def post(self, url, data):
        self.posts.append(dict(data))
        if self.fail:
            raise self.fail
        assert url == self.cfg.token_endpoint
        assert challenge_for(data["code_verifier"]) == self.challenge
        claims = {
            "iss": self.cfg.issuer,
            "aud": CLIENT,
            "exp": int(time.time()) + 600,
            "nbf": int(time.time()) - 60,
            "oid": "subject-1",
            "sub": "pairwise-1",
            "email": "person@example.org",
            "name": "A Person",
            "nonce": self.nonce,
            "roles": ["viewer"],
            **self.claims_extra,
        }
        return {"id_token": self.jwt(claims), "token_type": "Bearer"}

    def get(self, url):
        nums = self.priv.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": "k1",
                    "n": _b64(nums.n.to_bytes((nums.n.bit_length() + 7) // 8, "big")),
                    "e": _b64(nums.e.to_bytes((nums.e.bit_length() + 7) // 8, "big")),
                }
            ]
        }

    def jwt(self, claims):
        header = _b64(json.dumps({"alg": "RS256", "kid": "k1", "typ": "JWT"}).encode())
        payload = _b64(json.dumps(claims).encode())
        sig = self.priv.sign(f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{header}.{payload}.{_b64(sig)}"

    def observe(self, location):
        q = {k: v[0] for k, v in parse_qs(urlparse(location).query).items()}
        self.challenge = q["code_challenge"]
        self.nonce = q["nonce"]
        return q


def _client(cfg, idp, store, base="http://localhost:8912"):
    app = FastAPI()
    app.include_router(
        build_webgate_router(
            store,
            secure_cookies=False,
            oidc=[cfg],
            http=idp,
            jwks=JwksCache(idp, cfg.idp.jwks_uri),
        )
    )
    return TestClient(app, base_url=base, follow_redirects=False)


# ---------------------------------------------------------------- configuration


def test_a_public_client_reads_no_secret_setting(monkeypatch):
    def _never(*a, **k):
        raise AssertionError("a public client must not read the vault")

    monkeypatch.setattr(oidc, "_secret_from_vault", _never)
    cfg = OidcSignIn.from_env(PUBLIC_ENV)
    assert cfg.public and cfg.client_secret is None
    assert cfg.account_id == "subject" and cfg.idp_roles is False


@pytest.mark.parametrize("key", ["CLIENT_SECRET", "CLIENT_SECRET_FILE", "CLIENT_SECRET_VAULT"])
def test_a_secret_beside_a_public_client_is_refused(key):
    with pytest.raises(ValueError, match="takes no secret"):
        OidcSignIn.from_env({**PUBLIC_ENV, f"AXIOM_GATE_OIDC_{key}": "anything"})


def test_an_unknown_client_type_is_refused():
    with pytest.raises(ValueError, match="CLIENT_TYPE"):
        OidcSignIn.from_env({**PUBLIC_ENV, "AXIOM_GATE_OIDC_CLIENT_TYPE": "hybrid"})


# ---------------------------------------------------------------- the redirect


@pytest.mark.parametrize(
    ("base", "want"),
    [
        ("http://localhost:8912/", "http://localhost:8912/gate/oidc/callback"),
        ("http://127.0.0.1:8912/", "http://127.0.0.1:8912/gate/oidc/callback"),
        ("http://localhost/", "http://localhost/gate/oidc/callback"),
    ],
)
def test_the_redirect_is_the_loopback_form_with_the_browsers_port(base, want):
    assert _public().redirect_uri(base) == want
    assert _public().registered_redirect(base).endswith("/gate/oidc/callback")
    assert ":" not in _public().registered_redirect(base).removeprefix("http://")


def test_a_non_loopback_address_is_refused_with_the_fix():
    with pytest.raises(OidcSignInError) as caught:
        _public().redirect_uri("http://dev.example.org:8912/")
    assert "http://localhost:8912/" in caught.value.public


def test_a_confidential_client_keeps_its_own_address():
    cfg = OidcSignIn(idp=entra(TENANT), client_id=CLIENT, client_secret="s")
    assert cfg.redirect_uri("https://node.example.org/") == (
        "https://node.example.org/gate/oidc/callback"
    )


# ---------------------------------------------------------------- the flow


def test_the_flow_uses_pkce_s256_and_never_sends_a_secret():
    # Even a secret smuggled onto the config object is never sent for a public client.
    cfg = _public(client_secret="must-not-travel", idp_roles=False)
    idp = FakeIdp()
    store = InMemoryUserStore()
    c = _client(cfg, idp, store)

    r = c.get("/gate/oidc/login?next=/chat")
    assert r.status_code == 302
    q = idp.observe(r.headers["location"])
    assert q["code_challenge_method"] == "S256" and q["code_challenge"]
    assert q["redirect_uri"] == "http://localhost:8912/gate/oidc/callback"
    assert "client_secret" not in q

    r2 = c.get(f"/gate/oidc/callback?code=c&state={q['state']}")
    assert r2.status_code == 303 and r2.headers["location"] == "/chat"
    assert SESSION_COOKIE in r2.cookies
    (sent,) = idp.posts
    assert "client_secret" not in sent and "client_assertion" not in sent
    assert sent["redirect_uri"] == q["redirect_uri"]
    assert sent["client_id"] == CLIENT

    # Keyed on the IdP subject (Entra's oid), as the site keys it: the same principal.
    user = store.get_by_email("person@example.org")
    assert user.user_id == "subject-1"
    assert user.attributes["idp_subject"] == "subject-1"


def test_jit_on_a_dev_node_makes_the_owner(monkeypatch):
    monkeypatch.setenv("AXIOM_GATE_JIT_ROLE", "owner")
    cfg = _public(idp_roles=False)
    user = resolve_account(
        cfg, InMemoryUserStore(), {"oid": "s1", "email": "p@example.org", "roles": ["viewer"]}
    )
    assert user.roles == ("owner",)


def test_a_login_from_a_non_loopback_host_shows_the_fix_instead_of_crashing():
    c = _client(_public(), FakeIdp(), InMemoryUserStore(), base="http://dev.example.org:8912")
    r = c.get("/gate/oidc/login")
    assert "location" not in r.headers and "http://localhost:8912/" in r.text


# ---------------------------------------------------------------- identity


def _file_store(tmp_path, *users):
    """The accounts file a dev node keeps (`dev up` writes one, the gate reads it)."""
    from axiom.webauth import JsonFileUserStore, upsert_user_record

    path = tmp_path / "users.json"
    for u in users:
        upsert_user_record(path, **u)
    return JsonFileUserStore(path), path


SITE_CLAIMS = {
    "oid": "subject-9",
    "email": "first.last@example.edu",
    "preferred_username": "id123@eid.example.edu",
    "upn": "id123@eid.example.edu",
}


def test_the_sign_in_name_and_the_mail_address_are_one_account(tmp_path):
    """An institution gives one person a sign-in name and a mail address.

    The password account `dev up` made under the sign-in name is the one SSO
    lands in. It is re-keyed to the IdP subject, so the person is the same
    principal locally as on the site, and keeps its address, password and role.
    """
    from axiom.webauth import load_user_records

    store, path = _file_store(
        tmp_path,
        {"email": "id123@eid.example.edu", "password_hash": "h", "roles": ["owner"],
         "user_id": "id123@eid.example.edu"},
    )
    user = resolve_account(_public(idp_roles=False), store, SITE_CLAIMS)
    assert user.user_id == "subject-9"
    assert user.email == "id123@eid.example.edu"
    assert user.password_hash == "h" and user.roles == ("owner",)
    assert set(user.attributes["aliases"]) == {"first.last@example.edu", "id123@eid.example.edu"}
    assert [r["user_id"] for r in load_user_records(path)] == ["subject-9"]


def test_an_account_under_the_mail_address_is_found_from_the_sign_in_name_too(tmp_path):
    store, path = _file_store(
        tmp_path, {"email": "first.last@example.edu", "user_id": "first.last@example.edu",
         "password_hash": "h2"}
    )
    cfg = _public()
    assert resolve_account(cfg, store, SITE_CLAIMS).user_id == "subject-9"
    # The next sign-in lands there again, by subject, with no second account.
    assert resolve_account(cfg, store, SITE_CLAIMS).user_id == "subject-9"
    assert len(store) == 1


def test_the_local_principal_is_the_one_the_site_derives(tmp_path):
    """Same token, the site's (confidential) config and the dev node's (public)
    config: the same account id, so the same @handle."""
    site_cfg = OidcSignIn(idp=entra(TENANT), client_id=CLIENT, client_secret="s")
    site_user = resolve_account(site_cfg, InMemoryUserStore(), SITE_CLAIMS)
    dev_store, _ = _file_store(
        tmp_path, {"email": "first.last@example.edu", "user_id": "first.last@example.edu", "password_hash": "h2",
                   "roles": ["owner"]},
    )
    dev_user = resolve_account(_public(idp_roles=False), dev_store, SITE_CLAIMS)
    assert dev_user.user_id == site_user.user_id
    assert dev_user.handle == site_user.handle


# ---------------------------------------------------------------- errors name the fix


@pytest.mark.parametrize(
    ("error", "description"),
    [
        ("invalid_request", "AADSTS50011: The redirect URI specified in the request does not match"),
        ("redirect_uri_mismatch", ""),
        ("invalid_grant", "redirect_uri is not registered for this client"),
    ],
)
def test_a_redirect_the_provider_does_not_know_names_the_registration_fix(error, description):
    said = explain_idp_error(_public(), error, description, base_url="http://localhost:8912/")
    assert said == (
        "The site's app registration needs the loopback redirect "
        "http://localhost/gate/oidc/callback; ask the site admin."
    )


def test_a_provider_that_wants_a_secret_names_the_platform_fix():
    said = explain_idp_error(
        _public(), "invalid_client",
        "AADSTS7000218: The request body must contain client_assertion or client_secret",
        base_url="http://127.0.0.1:8912/",
    )
    assert "http://127.0.0.1/gate/oidc/callback" in said and "public client" in said


def test_an_unrecognized_error_is_not_explained():
    assert explain_idp_error(_public(), "server_error", "try later") is None


def test_the_callback_carries_the_named_fix_to_the_page():
    c = _client(_public(), FakeIdp(), InMemoryUserStore())
    r = c.get("/gate/oidc/login")
    q = parse_qs(urlparse(r.headers["location"]).query)
    r2 = c.get(
        "/gate/oidc/callback?error=invalid_request"
        "&error_description=AADSTS50011%3A+redirect+mismatch"
        f"&state={q['state'][0]}"
    )
    assert "needs the loopback redirect http://localhost/gate/oidc/callback" in r2.text


def test_a_token_exchange_refusal_names_the_fix():
    idp = FakeIdp(fail=IdpHttpError(400, "invalid_grant", "AADSTS50011: no match"))
    c = _client(_public(), idp, InMemoryUserStore())
    q = idp.observe(c.get("/gate/oidc/login").headers["location"])
    r = c.get(f"/gate/oidc/callback?code=c&state={q['state']}")
    assert "needs the loopback redirect" in r.text
    assert "client_secret" not in idp.posts[0]
