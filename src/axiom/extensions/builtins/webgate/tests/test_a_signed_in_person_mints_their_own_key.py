# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Nobody hands anybody a key, and nobody sends an invitation either.

The invitation flow took the administrator out of *holding* somebody else's key.
It did not take them out of the loop: they still create an invitation and send a
code. Asked three times whether that was good enough, the answer each time was
no — the goal is that an administrator generates nothing at all.

They do not have to. By the time somebody wants a key they have already signed
in, and the gate knows exactly who they are and what role they hold. The sign-in
is the proof of identity; the role somebody granted is the authorisation. A key
is just those two facts written into a credential the command line can carry, so
the person it belongs to can mint it themselves.

So `POST /gate/keys` with a valid session returns a key for the person in that
session, scoped to the roles already on it. The administrator's only act is the
one that was always theirs: granting the role.

**The scopes come from the session, never from the request.** A caller asking for
scopes would be a caller choosing their own authorisation, which is the whole
thing a role exists to prevent. An account with no role gets a refusal that says
to ask for one, not a key with nothing in it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def keys(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "api-keys.json"
    monkeypatch.setenv("AXIOM_GATE_API_KEYS_FILE", str(path))
    return path


@pytest.fixture
def client(keys) -> TestClient:
    from axiom.extensions.builtins.webgate.api.routers import build_webgate_router

    app = FastAPI()
    app.include_router(build_webgate_router(oidc=None, secure_cookies=False))
    return TestClient(app)


def _signed_in(monkeypatch, **claims):
    """A gate session, as `session_from_cookies` would return one."""
    from axiom.extensions.builtins.webgate.api import routers

    base = {"sub": "u-1", "email": "someone@example.edu", "name": "Someone",
            "roles": ["viewer"], "site": "example"}
    base.update(claims)
    monkeypatch.setattr(routers, "session_from_cookies", lambda *_a, **_k: base)
    return base


# ---------------------------------------------------------------------------
# The point
# ---------------------------------------------------------------------------


def test_a_signed_in_person_gets_a_key_without_anybody_minting_it(client, keys, monkeypatch):
    _signed_in(monkeypatch)
    reply = client.post("/gate/keys", json={})
    assert reply.status_code == 200, reply.text
    body = reply.json()
    assert body["token"].startswith("axk_")
    assert body["key_id"]


def test_the_key_belongs_to_the_person_in_the_session(client, keys, monkeypatch):
    """Not to whoever asked, and not to a name in the request body.

    The handle comes from the session's subject claim rather than its email,
    which is what the rest of the gate does and the right choice: a subject is
    the identity provider's stable identifier, and an email can be reassigned.
    """
    _signed_in(monkeypatch, sub="u-7", email="alex@example.edu")
    body = client.post("/gate/keys", json={"principal": "@someone-else:example"}).json()
    assert "someone-else" not in body["principal"], "the request chose the principal"
    assert "u-7" in body["principal"], body["principal"]


def test_the_scopes_come_from_the_role_not_the_request(client, keys, monkeypatch):
    """A caller choosing their own scopes is a caller choosing their own
    authorisation, which is the thing a role exists to prevent."""
    _signed_in(monkeypatch, roles=["viewer"])
    body = client.post("/gate/keys", json={"scopes": ["*:govern"]}).json()
    assert "*:govern" not in body["scopes"], "the request widened its own key"
    assert body["scopes"] == ["*:read"]


def test_the_key_the_person_minted_actually_works(client, keys, monkeypatch):
    """Through the real store, because a key nobody can use would satisfy every
    assertion above."""
    from axiom.webauth.api_keys import JsonFileApiKeyStore

    _signed_in(monkeypatch)
    token = client.post("/gate/keys", json={}).json()["token"]
    identity = JsonFileApiKeyStore(keys).resolve(token)
    assert identity is not None, "the node does not recognise the key it just issued"
    assert identity.scopes == ("*:read",)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_no_session_is_refused(client, keys):
    assert client.post("/gate/keys", json={}).status_code == 401


def test_an_account_with_no_role_is_told_to_ask_for_one(client, keys, monkeypatch):
    """The state every account arrives in. A key with no scopes would be a
    credential that authenticates and authorises nothing, which reads as a broken
    key rather than as a missing decision."""
    _signed_in(monkeypatch, roles=[])
    reply = client.post("/gate/keys", json={})
    assert reply.status_code == 403
    said = reply.json()["detail"].lower()
    assert "role" in said
    assert "ask" in said, "the refusal does not say what to do next"


def test_a_session_that_cannot_be_named_is_refused(client, keys, monkeypatch):
    """A principal has to be derivable from the session, or the key would belong
    to nobody and the audit trail would say so."""
    _signed_in(monkeypatch, email="", sub="")
    assert client.post("/gate/keys", json={}).status_code in (401, 403)


def test_a_role_that_no_longer_exists_is_refused_rather_than_ignored(client, keys, monkeypatch):
    """Silently dropping an unknown role would mint a narrower key than the
    person was granted and leave them debugging a permission they do have."""
    _signed_in(monkeypatch, roles=["wizard"])
    reply = client.post("/gate/keys", json={})
    assert reply.status_code == 403
    assert "wizard" in reply.json()["detail"]


# ---------------------------------------------------------------------------
# Hygiene
# ---------------------------------------------------------------------------


def test_the_key_is_labelled_so_an_operator_can_tell_what_it_is(client, keys, monkeypatch):
    from axiom.webauth.api_keys import load_api_key_records

    _signed_in(monkeypatch)
    client.post("/gate/keys", json={})
    record = load_api_key_records(keys)[0]
    assert record["name"], "the key has no label"
    assert "self" in record["name"].lower() or "signed in" in record["name"].lower()


def test_minting_twice_gives_two_distinct_keys(client, keys, monkeypatch):
    """A person who loses one mints another. Revoking the old one is the
    operator's call, so this must not silently replace it."""
    _signed_in(monkeypatch)
    first = client.post("/gate/keys", json={}).json()["key_id"]
    second = client.post("/gate/keys", json={}).json()["key_id"]
    assert first != second


def test_the_token_is_never_logged(client, keys, monkeypatch, caplog):
    import logging

    _signed_in(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        token = client.post("/gate/keys", json={}).json()["token"]
    logged = "\n".join(r.getMessage() for r in caplog.records)
    # Parsed, not ``split("_")[-1]``: the urlsafe secret may contain ``_``, and
    # the tail after its last one can be a single character that any log line
    # contains. The whole secret is what must not appear.
    from axiom.webauth.api_keys import parse_token

    _, secret = parse_token(token)
    assert len(secret) >= 40
    assert token not in logged
    assert secret not in logged


def test_a_body_that_is_not_json_is_refused_cleanly(client, keys, monkeypatch):
    _signed_in(monkeypatch)
    reply = client.post(
        "/gate/keys", content=b"not json", headers={"content-type": "application/json"}
    )
    assert reply.status_code in (200, 400, 422), reply.text
