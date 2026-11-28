# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A contributor's local node signs in with the site's SSO, with no secret on the laptop (#1717).

The vault may hold a confidential client (the app owner's), a public one (a
client id, nothing secret), or none. On an install bound to a site node with
none, `dev up` asks the bound node for its public client, records it, and
offers SSO. Everything here is a fake: no node, no vault, no identity provider.
"""

from __future__ import annotations

from types import SimpleNamespace

from axiom.extensions.builtins.dev import cli, signin
from axiom.extensions.builtins.dev.skills import up

TENANT = "00000000-0000-0000-0000-0000000000aa"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"
DEV_APP = "00000000-0000-0000-0000-0000000000dd"
#: Captured at import, before the harness fixture replaces it with "not bound".
REAL_FROM_BOUND_SITE = signin.from_bound_site
BOUND = SimpleNamespace(bound=True, face_url="https://node.example.org", site="example-site")


def _confidential(name="owner-sso"):
    return {"name": name, "issuer_url": ISSUER, "client_id": "c-1", "fingerprint": "f"}


def _public(name="dev-sso", **extra):
    return {"name": name, "issuer_url": ISSUER, "client_id": DEV_APP,
            "client_type": "public", "fingerprint": "f", **extra}


# ---------------------------------------------------------------- detection


def test_a_declared_public_client_is_public():
    client, _ = signin.detect([_public()])
    assert client.public


def test_an_entry_with_no_value_recorded_is_public():
    entry = {"name": "x", "issuer_url": ISSUER, "client_id": DEV_APP}
    client, _ = signin.detect([entry])
    assert client.public


def test_a_confidential_client_is_not_public():
    client, _ = signin.detect([_confidential()])
    assert not client.public


def test_none_is_none():
    assert signin.detect([{"name": "git-token", "fingerprint": "f"}]) == (None, [])


def test_a_confidential_client_wins_over_a_public_one():
    client, notes = signin.detect([_public(), _confidential()])
    assert client.credential == "owner-sso" and not client.public and notes == []


def test_a_public_client_names_no_secret_to_the_node():
    env = signin.detect([_public()])[0].environment()
    assert "AXIOM_GATE_OIDC_CLIENT_SECRET_VAULT" not in env
    assert not any("SECRET" in k for k in env)
    assert env["AXIOM_GATE_OIDC_CLIENT_TYPE"] == "public"
    assert env["AXIOM_GATE_OIDC_CLIENT_ID"] == DEV_APP
    assert "AXIOM_GATE_OIDC_ACCOUNT_ID" not in env  # keyed on the subject, as the site keys it
    assert env["AXIOM_GATE_JIT_ROLE"] == "owner"


def test_a_confidential_client_still_names_its_secret_by_name():
    env = signin.detect([_confidential()])[0].environment()
    assert env["AXIOM_GATE_OIDC_CLIENT_SECRET_VAULT"] == "owner-sso"
    assert "AXIOM_GATE_OIDC_CLIENT_TYPE" not in env


# ---------------------------------------------------------------- asking the bound site


class FakeNode:
    def __init__(self, status=200, body=None):
        self.status = status
        self.body = body if body is not None else {
            "issuer_url": ISSUER, "client_id": DEV_APP, "client_type": "public",
            "allowed_redirects": ["http://localhost/gate/oidc/callback"],
        }
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers)))
        return self.status, self.body


def test_the_bound_node_is_asked_with_the_bindings_key():
    node = FakeNode()
    offer, notes = signin.from_bound_site(mode=BOUND, http_get=node, key="axk_test")
    assert offer == signin.SiteOffer("example-site", ISSUER, DEV_APP) and notes == []
    url, headers = node.calls[0]
    assert url == "https://node.example.org/api/v1/site/sign-in"
    assert headers["authorization"] == "Bearer axk_test"


def test_an_unbound_install_asks_nobody():
    node = FakeNode()
    unbound = SimpleNamespace(bound=False, face_url="", site="")
    assert signin.from_bound_site(mode=unbound, http_get=node, key="k") == (None, [])
    assert node.calls == []


def test_a_site_that_has_not_enabled_it_says_so():
    offer, notes = signin.from_bound_site(mode=BOUND, http_get=FakeNode(404, {}), key="k")
    assert offer is None
    assert notes == [
        "this site has not enabled SSO for local development; using password sign-in"
    ]


def test_a_refused_key_says_what_it_needs():
    offer, notes = signin.from_bound_site(mode=BOUND, http_get=FakeNode(403, {}), key="k")
    assert offer is None and "HTTP 403" in notes[0] and "read access" in notes[0]


def test_an_answer_that_is_not_a_public_client_is_not_used():
    body = {"issuer_url": ISSUER, "client_id": DEV_APP, "client_type": "confidential"}
    offer, notes = signin.from_bound_site(mode=BOUND, http_get=FakeNode(200, body), key="k")
    assert offer is None and "not a public client" in notes[0]


def test_an_unreachable_node_is_said_not_raised():
    def boom(*a):
        raise OSError("no route")

    offer, notes = signin.from_bound_site(mode=BOUND, http_get=boom, key="k")
    assert offer is None and "password sign-in" in notes[0]


# ---------------------------------------------------------------- dev up


def _params(**kw):
    return {"email": "person@example.org", "db_url": "postgresql://u@127.0.0.1:5432/dev", **kw}


def test_dev_up_on_a_bound_install_records_the_public_client_and_offers_sso(
    harness, monkeypatch
):
    node = FakeNode()
    real = REAL_FROM_BOUND_SITE
    monkeypatch.setattr(
        signin, "from_bound_site", lambda **kw: real(mode=BOUND, http_get=node, key="k")
    )
    result = up.run(_params(), harness["ctx"])
    assert result.ok, result.errors

    # Recorded in the vault: a public entry whose value is the (public) client id.
    vault = harness["vault"]
    name = signin.entry_name("example-site")
    assert vault.values[name] == DEV_APP.encode()
    assert vault.meta[name]["client_type"] == "public"
    assert vault.meta[name]["issuer_url"] == ISSUER

    # The node is told the public client and no secret.
    env = harness["started"][0].env
    assert env["AXIOM_GATE_OIDC_CLIENT_TYPE"] == "public"
    assert env["AXIOM_GATE_OIDC_CLIENT_ID"] == DEV_APP
    assert not any(k.startswith("AXIOM_GATE_OIDC_CLIENT_SECRET") for k in env)

    printed = cli._render("up", result)
    assert "Sign in: SSO via login.microsoftonline.com (from the bound site)" in printed
    assert "or with a password as person@example.org" in printed


def test_dev_up_says_why_when_the_site_has_not_enabled_it(harness, monkeypatch):
    real = REAL_FROM_BOUND_SITE
    monkeypatch.setattr(
        signin, "from_bound_site",
        lambda **kw: real(mode=BOUND, http_get=FakeNode(404, {}), key="k"),
    )
    result = up.run(_params(), harness["ctx"])
    assert result.ok and result.value["sign_in"] is None
    printed = cli._render("up", result)
    assert "this site has not enabled SSO for local development; using password sign-in" in printed
    assert "AXIOM_GATE_OIDC_CLIENT_ID" not in harness["started"][0].env


def test_sign_in_none_forces_password_and_asks_nobody(harness, monkeypatch):
    def never(**kw):
        raise AssertionError("--sign-in none must not ask the site")

    monkeypatch.setattr(signin, "from_bound_site", never)
    result = up.run(_params(sign_in="none"), harness["ctx"])
    assert result.ok and result.value["sign_in"] is None


def test_a_confidential_client_in_the_vault_wins_and_the_site_is_not_asked(
    harness, monkeypatch
):
    def never(**kw):
        raise AssertionError("the owner's confidential client wins")

    monkeypatch.setattr(signin, "from_bound_site", never)
    harness["vault"].rows = [_confidential()]
    result = up.run(_params(), harness["ctx"])
    assert result.ok
    env = harness["started"][0].env
    assert env["AXIOM_GATE_OIDC_CLIENT_SECRET_VAULT"] == "owner-sso"
    assert result.value["sign_in_public"] is False


def test_the_real_vault_records_a_public_entry_that_detects_as_public(tmp_path, monkeypatch):
    from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore

    monkeypatch.setenv("AXIOM_FOREIGN_SECRETS_BACKEND", "file")
    vault = ForeignCredentialStore(tmp_path)
    recorded = signin.record_offer(vault, signin.SiteOffer("example-site", ISSUER, DEV_APP))
    assert recorded.public
    client, notes = signin.detect(vault.list())
    assert client.public and client.client_id == DEV_APP and notes == []


def test_a_public_client_entered_by_hand_is_recorded_and_offered(harness):
    """`dev up --sign-in-client <issuer> <client_id>`, for a site node that does
    not publish one yet: recorded as a public client, then offered as SSO."""
    bad = up.run(_params(sign_in_client=["http://id.example.org", DEV_APP]), harness["ctx"])
    assert not bad.ok and "https issuer" in bad.errors[0]

    result = up.run(_params(sign_in_client=[ISSUER, DEV_APP]), harness["ctx"])
    assert result.ok, result.errors
    name = signin.entry_name("login.microsoftonline.com")
    meta = harness["vault"].meta[name]
    assert meta["client_type"] == "public" and meta["client_id"] == DEV_APP
    assert meta["notes"] == "public sign-in client entered by hand (not a secret)"
    env = harness["started"][-1].env
    assert env["AXIOM_GATE_OIDC_CLIENT_TYPE"] == "public"
    assert not any(k.startswith("AXIOM_GATE_OIDC_CLIENT_SECRET") for k in env)
    assert "Sign in: SSO via login.microsoftonline.com" in cli._render("up", result)
