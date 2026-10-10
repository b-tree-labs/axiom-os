"""A local node offers the sign-in client its owner's vault already holds.

The client secret's *name* is what reaches the node, never its value.
"""

from __future__ import annotations

from axiom.extensions.builtins.dev import signin

TENANT = "00000000-0000-0000-0000-00000000000a"


def _entry(name, issuer, client_id="client-1"):
    return {"name": name, "issuer_url": issuer, "client_id": client_id}


def test_a_tenant_issuer_becomes_the_tenant_preset():
    client, notes = signin.detect(
        [_entry("sso", f"https://login.microsoftonline.com/{TENANT}/v2.0")]
    )
    env = client.environment()
    assert env == {
        "AXIOM_GATE_OIDC_PROVIDER": "entra",
        "AXIOM_GATE_OIDC_TENANT": TENANT,
        "AXIOM_GATE_OIDC_CLIENT_ID": "client-1",
        "AXIOM_GATE_OIDC_CLIENT_SECRET_VAULT": "sso",
    }
    assert notes == []


def test_any_other_https_issuer_is_used_by_discovery():
    client, _ = signin.detect([_entry("idp", "https://id.example.org")])
    assert client.environment()["AXIOM_GATE_OIDC_PROVIDER"] == "https://id.example.org"
    assert "AXIOM_GATE_OIDC_TENANT" not in client.environment()


def test_no_value_ever_appears_in_what_the_node_is_given():
    client, _ = signin.detect([{**_entry("idp", "https://id.example.org"), "fingerprint": "abc"}])
    assert all("abc" not in v for v in client.environment().values())


def test_a_credential_without_a_client_id_is_not_a_sign_in_client():
    entries = [{"name": "git-token", "issuer_url": "https://git.example.org"}]
    assert signin.detect(entries) == (None, [])


def test_a_plain_http_issuer_is_not_offered():
    assert signin.detect([_entry("idp", "http://id.example.org")]) == (None, [])


def test_two_clients_are_not_guessed_between():
    client, notes = signin.detect(
        [_entry("a", "https://id.example.org"), _entry("b", "https://other.example.org")]
    )
    assert client is None
    assert "a, b" in notes[0] and "--sign-in" in notes[0]


def test_one_can_be_named_when_there_are_several():
    entries = [_entry("a", "https://id.example.org"), _entry("b", "https://other.example.org")]
    assert signin.choose(entries, "b").credential == "b"
    assert signin.choose(entries, "missing") is None
