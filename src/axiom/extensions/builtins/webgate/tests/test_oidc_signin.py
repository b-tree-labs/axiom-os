# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""OIDC sign-in on the gate: "Sign in with <IdP>" → the same session as a password.

A fake IdP (an injected http client + a self-generated RSA key) plays both legs
without the network. The guarantees driven here: PKCE/state/nonce are real, a
forged or foreign id_token is refused, the account is keyed on the immutable
subject, a pre-existing password account is linked rather than duplicated, and
what comes out is an ordinary gate session that ``/gate/verify`` honours.
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
from axiom.extensions.builtins.webgate.api.routers import build_webgate_router
from axiom.extensions.builtins.webgate.oidc import (
    TXN_COOKIE,
    JwksCache,
    OidcSignIn,
    OidcSignInError,
    resolve_account,
)
from axiom.webauth import SESSION_COOKIE, get_password_hash
from axiom.webauth.keys import reset_key_store_for_tests
from axiom.webauth.users import InMemoryUserStore, User

warnings.filterwarnings("ignore")

TENANT = "31d7e2a5-0000-0000-0000-000000000000"
CLIENT_ID = "app-client-123"
IDP = entra(TENANT)
OID = "8f2d0c1e-5a3b-4c6d-9e7f-0123456789ab"


@pytest.fixture(autouse=True)
def _keys():
    reset_key_store_for_tests()
    yield
    reset_key_store_for_tests()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class FakeIdp:
    """Both IdP endpoints the flow touches, plus the JWKS document."""

    def __init__(self, kid: str = "k1") -> None:
        self.priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = kid
        self.token_calls: list[dict] = []
        self.next_claims: dict = {}
        self.issued_nonce_override: str | None = None
        self.jwks_fetches = 0
        self.token_error: Exception | None = None

    # -- the http client the flow is given
    def post(self, url: str, data: dict) -> dict:
        assert url == IDP.token_endpoint
        self.token_calls.append(dict(data))
        if self.token_error:
            raise self.token_error
        # The IdP checks the PKCE verifier against the challenge it saw in leg 1.
        assert challenge_for(data["code_verifier"]) == self.expected_challenge
        assert data["code"] == "good-code"
        claims = {
            "iss": IDP.issuer,
            "aud": CLIENT_ID,
            "exp": int(time.time()) + 600,
            "nbf": int(time.time()) - 60,
            "sub": "sub-opaque",
            "oid": OID,
            "email": "Alice@Example.EDU",
            "name": "Alice Longhorn",
            "nonce": self.issued_nonce_override or self.nonce_seen,
            **self.next_claims,
        }
        return {"id_token": self.jwt(claims), "access_token": "at", "token_type": "Bearer"}

    def get(self, url: str) -> dict:
        assert url == IDP.jwks_uri
        self.jwks_fetches += 1
        return self.jwks()

    # -- helpers
    def jwks(self) -> dict:
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

    def jwt(self, claims: dict, kid: str | None = None, priv=None) -> str:
        header = _b64(json.dumps({"alg": "RS256", "kid": kid or self.kid, "typ": "JWT"}).encode())
        payload = _b64(json.dumps(claims).encode())
        signer = priv or self.priv
        sig = signer.sign(f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{header}.{payload}.{_b64(sig)}"

    def observe_leg1(self, location: str) -> dict:
        q = {k: v[0] for k, v in parse_qs(urlparse(location).query).items()}
        self.expected_challenge = q["code_challenge"]
        self.nonce_seen = q["nonce"]
        return q


def _cfg(**over) -> OidcSignIn:
    base = dict(idp=IDP, client_id=CLIENT_ID, label="Sign in with your institution")
    base.update(over)
    return OidcSignIn(**base)


def _client(store=None, *, idp: FakeIdp, cfg: OidcSignIn | None = None):
    app = FastAPI()
    store = store if store is not None else InMemoryUserStore()
    cfg = cfg or _cfg()
    app.include_router(
        build_webgate_router(
            store,
            secure_cookies=False,
            oidc=cfg,
            http=idp,
            jwks=JwksCache(idp, IDP.jwks_uri),
        )
    )
    return TestClient(app, base_url="http://gate.example", follow_redirects=False), store


def _leg1(c, idp: FakeIdp, next_="/chat", remember=False):
    r = c.get(f"/gate/oidc/login?next={next_}" + ("&remember=1" if remember else ""))
    assert r.status_code == 302
    q = idp.observe_leg1(r.headers["location"])
    assert TXN_COOKIE in r.cookies
    return q


def _leg2(c, q, code="good-code", state=None):
    return c.get(f"/gate/oidc/callback?code={code}&state={state or q['state']}")


# ---------------------------------------------------------------- the button


def test_login_page_offers_sso_when_configured():
    c, _ = _client(idp=FakeIdp())
    r = c.get("/gate/login?next=/chat")
    assert r.status_code == 200
    assert "Sign in with your institution" in r.text
    assert 'href="/gate/oidc/login?next=/chat"' in r.text
    assert 'name="password"' in r.text  # the password form stays


def test_login_page_has_no_sso_when_not_configured():
    app = FastAPI()
    app.include_router(build_webgate_router(InMemoryUserStore(), secure_cookies=False, oidc=None))
    c = TestClient(app, base_url="http://gate.example")
    r = c.get("/gate/login")
    assert "/gate/oidc/login" not in r.text
    assert c.get("/gate/oidc/login").status_code == 404


# ---------------------------------------------------------------- leg 1


def test_leg1_redirects_to_idp_with_pkce_state_nonce():
    idp = FakeIdp()
    c, _ = _client(idp=idp)
    r = c.get("/gate/oidc/login?next=/chat")
    assert r.status_code == 302
    u = urlparse(r.headers["location"])
    assert f"{u.scheme}://{u.netloc}{u.path}" == IDP.authorization_endpoint
    q = {k: v[0] for k, v in parse_qs(u.query).items()}
    assert q["response_type"] == "code"
    assert q["client_id"] == CLIENT_ID
    assert q["redirect_uri"] == "http://gate.example/gate/oidc/callback"
    assert q["code_challenge_method"] == "S256"
    assert len(q["code_challenge"]) >= 43
    assert len(q["state"]) >= 24 and len(q["nonce"]) >= 24
    assert set(q["scope"].split()) == {"openid", "profile", "email"}
    # The verifier never appears in the URL; it lives in the signed txn cookie.
    assert "code_verifier" not in q
    assert SESSION_COOKIE not in r.cookies


def test_leg1_refuses_open_redirect_in_next():
    idp = FakeIdp()
    c, _ = _client(idp=idp)
    q = _leg1(c, idp, next_="https://evil.example/")
    r = _leg2(c, q)
    assert r.status_code == 303
    assert r.headers["location"] == "/"


# ---------------------------------------------------------------- leg 2, happy path


def test_full_sign_in_creates_account_keyed_on_oid_and_issues_gate_session():
    idp = FakeIdp()
    idp.next_claims = {"roles": ["Researcher"]}
    c, store = _client(idp=idp)
    q = _leg1(c, idp, next_="/chat")
    r = _leg2(c, q)
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/chat"
    assert SESSION_COOKIE in r.cookies
    assert r.cookies.get(TXN_COOKIE) in (None, "")  # transaction cookie cleared

    # The account exists, keyed on the immutable subject, SSO-only.
    u = store.get_by_id(OID)
    assert u is not None
    assert u.email == "alice@example.edu"
    assert u.name == "Alice Longhorn"
    assert u.roles == ("Researcher",)
    assert u.password_hash is None
    assert u.attributes["idp"] == "entra" and u.attributes["idp_subject"] == OID

    # …and it is an ordinary gate session the forward-auth contract honours.
    v = c.get("/gate/verify")
    assert v.status_code == 200
    assert v.headers["X-Axiom-User-Id"] == OID
    assert v.headers["X-Axiom-User-Email"] == "alice@example.edu"
    assert v.headers["X-Axiom-User-Roles"] == "Researcher"

    # PKCE went to the IdP: the verifier matched the leg-1 challenge (asserted in
    # FakeIdp.post) and no client secret was sent for a public client.
    assert "client_secret" not in idp.token_calls[0]


def test_client_secret_is_sent_when_configured():
    idp = FakeIdp()
    c, _ = _client(idp=idp, cfg=_cfg(client_secret="s3cret"))
    q = _leg1(c, idp)
    assert _leg2(c, q).status_code == 303
    assert idp.token_calls[0]["client_secret"] == "s3cret"


def test_remember_extends_the_session_cookie():
    idp = FakeIdp()
    c, _ = _client(idp=idp)
    q = _leg1(c, idp, remember=True)
    r = _leg2(c, q)
    set_cookie = r.headers.get("set-cookie", "")
    # 30 days, not the 12-hour default
    assert f"{SESSION_COOKIE}=" in set_cookie and "Max-Age=2592000" in set_cookie


def test_sso_only_account_cannot_use_the_password_form():
    idp = FakeIdp()
    c, store = _client(idp=idp)
    _leg2(c, _leg1(c, idp))
    r = c.post(
        "/gate/login", data={"email": "alice@example.edu", "password": "anything", "next": "/"}
    )
    assert r.status_code == 401


# ---------------------------------------------------------------- linking + JIT


def test_existing_password_account_is_linked_not_duplicated():
    idp = FakeIdp()  # no roles claim this time
    store = InMemoryUserStore(
        [
            User(
                user_id="alice-local",
                email="alice@example.edu",
                password_hash=get_password_hash("pw-000000000000000"),
                name="A. Local",
                roles=("operator",),
            )
        ]
    )
    c, store = _client(store, idp=idp)
    r = _leg2(c, _leg1(c, idp))
    assert r.status_code == 303
    assert len(store) == 1
    u = store.get_by_email("alice@example.edu")
    assert u.user_id == "alice-local"  # the local id survives
    assert u.roles == ("operator",)  # no roles claim → local roles kept
    assert u.name == "Alice Longhorn"  # IdP's display name refreshes
    assert u.password_hash is not None  # password path still works
    assert u.attributes["idp_subject"] == OID
    assert c.get("/gate/verify").headers["X-Axiom-User-Id"] == "alice-local"


def test_roles_claim_overrides_local_roles_when_present():
    idp = FakeIdp()
    idp.next_claims = {"roles": ["Student"]}
    store = InMemoryUserStore(
        [
            User(
                user_id=OID,
                email="alice@example.edu",
                roles=("operator",),
                attributes={"idp": "entra"},
            )
        ]
    )
    c, store = _client(store, idp=idp)
    assert _leg2(c, _leg1(c, idp)).status_code == 303
    assert store.get_by_id(OID).roles == ("Student",)


def test_disabled_account_is_refused_even_via_sso():
    idp = FakeIdp()
    store = InMemoryUserStore([User(user_id=OID, email="alice@example.edu", disabled=True)])
    c, _ = _client(store, idp=idp)
    r = _leg2(c, _leg1(c, idp))
    assert r.status_code == 401
    assert "disabled" in r.text
    assert SESSION_COOKIE not in r.cookies


def test_jit_account_without_roles_claim_gets_the_default_role(monkeypatch):
    monkeypatch.delenv("AXIOM_GATE_JIT_ROLE", raising=False)
    idp = FakeIdp()  # no roles claim
    c, store = _client(idp=idp)
    assert _leg2(c, _leg1(c, idp)).status_code == 303
    assert store.get_by_id(OID).roles == ("viewer",)


def test_jit_default_role_is_env_configurable(monkeypatch):
    monkeypatch.setenv("AXIOM_GATE_JIT_ROLE", "member")
    idp = FakeIdp()
    c, store = _client(idp=idp)
    assert _leg2(c, _leg1(c, idp)).status_code == 303
    assert store.get_by_id(OID).roles == ("member",)


def test_jit_default_role_never_overrides_an_idp_roles_claim(monkeypatch):
    monkeypatch.setenv("AXIOM_GATE_JIT_ROLE", "member")
    idp = FakeIdp()
    idp.next_claims = {"roles": ["Researcher"]}
    c, store = _client(idp=idp)
    assert _leg2(c, _leg1(c, idp)).status_code == 303
    assert store.get_by_id(OID).roles == ("Researcher",)


def test_jit_off_refuses_unknown_subject():
    idp = FakeIdp()
    c, store = _client(idp=idp, cfg=_cfg(jit=False))
    r = _leg2(c, _leg1(c, idp))
    assert r.status_code == 401
    assert "not been provisioned" in r.text
    assert len(store) == 0


def test_read_only_store_cannot_jit():
    class ReadOnly:
        def get_by_email(self, email):
            return None

        def get_by_id(self, user_id):
            return None

    with pytest.raises(OidcSignInError) as ei:
        resolve_account(_cfg(), ReadOnly(), {"oid": OID, "email": "a@b.org"})
    assert "cannot be created" in ei.value.public
    assert "not writable" in ei.value.detail


# ---------------------------------------------------------------- refusals


def test_state_mismatch_is_refused():
    idp = FakeIdp()
    c, _ = _client(idp=idp)
    q = _leg1(c, idp)
    r = _leg2(c, q, state="not-the-state")
    assert r.status_code == 401
    assert SESSION_COOKIE not in r.cookies
    assert idp.token_calls == []  # never even asked the IdP


def test_nonce_mismatch_is_refused():
    idp = FakeIdp()
    idp.issued_nonce_override = "replayed-nonce"
    c, _ = _client(idp=idp)
    r = _leg2(c, _leg1(c, idp))
    assert r.status_code == 401
    assert SESSION_COOKIE not in r.cookies


def test_forged_id_token_is_refused():
    idp = FakeIdp()
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    real_post = idp.post

    def forged(url, data):
        resp = real_post(url, data)
        payload = json.loads(base64.urlsafe_b64decode(resp["id_token"].split(".")[1] + "=="))
        resp["id_token"] = idp.jwt(payload, priv=other)  # right claims, wrong key
        return resp

    idp.post = forged
    c, store = _client(idp=idp)
    r = _leg2(c, _leg1(c, idp))
    assert r.status_code == 401
    assert len(store) == 0


def test_wrong_audience_is_refused():
    idp = FakeIdp()
    idp.next_claims = {"aud": "someone-else"}
    c, store = _client(idp=idp)
    assert _leg2(c, _leg1(c, idp)).status_code == 401
    assert len(store) == 0


def test_idp_error_response_is_refused_without_calling_token_endpoint():
    idp = FakeIdp()
    c, _ = _client(idp=idp)
    _leg1(c, idp)
    r = c.get("/gate/oidc/callback?error=access_denied&error_description=user+cancelled")
    assert r.status_code == 401
    assert "not completed" in r.text
    assert idp.token_calls == []


def test_callback_without_transaction_is_refused():
    idp = FakeIdp()
    c, _ = _client(idp=idp)
    r = c.get("/gate/oidc/callback?code=good-code&state=x")
    assert r.status_code == 401
    assert "expired" in r.text


def test_session_token_cannot_stand_in_for_the_transaction():
    # A valid gate session cookie presented as the txn cookie must be refused by type.
    idp = FakeIdp()
    store = InMemoryUserStore(
        [User(user_id="u1", email="u@x.org", password_hash=get_password_hash("pw-000000000000000"))]
    )
    c, _ = _client(store, idp=idp)
    c.post("/gate/login", data={"email": "u@x.org", "password": "pw-000000000000000", "next": "/"})
    session = c.cookies.get(SESSION_COOKIE)
    c.cookies.set(TXN_COOKIE, session)
    r = c.get("/gate/oidc/callback?code=good-code&state=x")
    assert r.status_code == 401


def test_token_endpoint_failure_is_a_clean_refusal():
    idp = FakeIdp()
    idp.token_error = RuntimeError("boom: secret-looking-string")
    c, _ = _client(idp=idp)
    r = _leg2(c, _leg1(c, idp))
    assert r.status_code == 401
    assert "secret-looking-string" not in r.text  # detail never reaches the browser


def test_missing_email_claim_is_refused():
    idp = FakeIdp()
    idp.next_claims = {"email": None, "preferred_username": "no-at-sign"}
    c, store = _client(idp=idp)
    assert _leg2(c, _leg1(c, idp)).status_code == 401
    assert len(store) == 0


# ---------------------------------------------------------------- JWKS + config


def test_jwks_is_cached_and_refetched_once_on_unknown_kid():
    idp = FakeIdp(kid="k1")
    cache = JwksCache(idp, IDP.jwks_uri)
    cache.get("k1")
    cache.get("k1")
    assert idp.jwks_fetches == 1
    cache.get("k2")  # rotation: unknown kid → one refetch
    assert idp.jwks_fetches == 2


def test_from_env_unset_means_password_only():
    assert OidcSignIn.from_env({}) is None


def test_from_env_entra(tmp_path):
    secret_file = tmp_path / "gate-oidc.secret"
    secret_file.write_text("shh\n")
    cfg = OidcSignIn.from_env(
        {
            "AXIOM_GATE_OIDC_PROVIDER": "entra",
            "AXIOM_GATE_OIDC_TENANT": TENANT,
            "AXIOM_GATE_OIDC_CLIENT_ID": CLIENT_ID,
            "AXIOM_GATE_OIDC_CLIENT_SECRET_FILE": str(secret_file),
            "AXIOM_GATE_OIDC_LABEL": "Sign in with your institution",
            "AXIOM_GATE_OIDC_JIT": "0",
        }
    )
    assert cfg.idp.issuer == IDP.issuer
    assert cfg.client_id == CLIENT_ID
    assert cfg.client_secret == "shh"
    assert cfg.label == "Sign in with your institution"
    assert cfg.subject_claim == "oid"
    assert cfg.jit is False


@pytest.mark.parametrize(
    "env",
    [
        {"AXIOM_GATE_OIDC_PROVIDER": "entra"},  # no client id
        {"AXIOM_GATE_OIDC_CLIENT_ID": "x"},  # no provider
        {"AXIOM_GATE_OIDC_PROVIDER": "entra", "AXIOM_GATE_OIDC_CLIENT_ID": "x"},  # no tenant
        {"AXIOM_GATE_OIDC_PROVIDER": "okta", "AXIOM_GATE_OIDC_CLIENT_ID": "x"},  # unknown
    ],
)
def test_from_env_half_configured_is_refused_loudly(env):
    with pytest.raises(ValueError):
        OidcSignIn.from_env(env)


def test_spa_brand_payload_carries_sso(tmp_path):
    from axiom.extensions.builtins.webgate.api.routers import LoginBrand, _spa_brand_payload

    payload = _spa_brand_payload(LoginBrand(product_name="Acme Research"), _cfg())
    assert payload["sso"] == {"url": "/gate/oidc/login", "label": "Sign in with your institution"}
    assert "sso" not in _spa_brand_payload(LoginBrand())
