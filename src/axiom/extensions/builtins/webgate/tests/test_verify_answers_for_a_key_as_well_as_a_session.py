# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`GET /gate/verify` answers for an API key, not only a browser session.

The forward-auth endpoint read `session_from_cookies` and nothing else, so a
service in front of nginx could authenticate a person and not a key. The
consequence showed up on a running node: a chat face with no way to verify an API
caller trusted a `X-OpenWebUI-User-Email` header that any caller could set.

The library path (`build_bearer_resolver` + `build_authz_hook`) already verified
keys, but it needs the axiom package in the calling process, and that face runs in
an environment without it. Vendoring key parsing and scrypt a second time is the
worse of the two options, so the gate answers for both families and nginx fronts
the service.

**The header contract is the deliverable**, because somebody builds a front door
against it:

| header | session | API key |
|---|---|---|
| `X-Axiom-Auth` | `session` | `api-key` |
| `X-Axiom-User-Id` | the subject claim | the principal handle `@name:site` |
| `X-Axiom-User-Email` | the email claim | **empty** — a key has no address |
| `X-Axiom-User-Name` | the name claim | **empty** — a key's label is not a person |
| `X-Axiom-User-Roles` | the roles claim | **empty** — see below |
| `X-Axiom-User-Site` | the site claim | the key's bound site |
| `X-Axiom-User-Scopes` | empty | the key's verified scopes, comma-separated |
| `X-Axiom-Key-Id` | empty | the key id, for an incident to grep |

`Roles` is empty for a key and that is not an oversight. A role is resolved
through the bundles **when the key is issued** and only the resulting scopes are
stored, so the node cannot say afterwards which role a key came from. Reporting a
guess would be worse than reporting nothing. Gate on `Scopes`.

**A presented bearer never falls back to the cookie.** An invalid or revoked key
is 401 even when a valid session cookie rides along, because the alternative is a
caller whose key was revoked continuing to work through a cookie they also happen
to hold, and nobody looking at the key would understand why.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from axiom.webauth.api_keys import append_api_key_record, mint_api_key  # noqa: E402


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


def _key(keys: Path, *, principal="@someone:example", scopes=("chat:invoke", "rag:read"),
         site="example", name="chat client") -> str:
    token, record = mint_api_key(principal=principal, scopes=scopes, name=name, site=site)
    append_api_key_record(keys, record)
    return token


def _bearer(token: str) -> dict:
    return {"authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


def test_a_valid_key_verifies(client, keys):
    reply = client.get("/gate/verify", headers=_bearer(_key(keys)))
    assert reply.status_code == 200, reply.text


def test_the_principal_handle_is_the_identity(client, keys):
    """What the caller downstream keys on. A key has no email, so this is the
    only stable identity it carries."""
    reply = client.get("/gate/verify", headers=_bearer(_key(keys)))
    assert reply.headers["X-Axiom-User-Id"] == "@someone:example"


def test_the_email_header_is_empty_rather_than_a_handle(client, keys):
    """A handle is not an address. Putting one in an email header invites
    something downstream to send mail to it, or to match it against a directory
    and find nothing."""
    reply = client.get("/gate/verify", headers=_bearer(_key(keys)))
    assert reply.headers["X-Axiom-User-Email"] == ""


def test_the_name_header_is_empty_rather_than_the_keys_label(client, keys):
    """A key's label says what the key is for — "chat client". Rendered as a
    name it reads as the person's."""
    reply = client.get("/gate/verify", headers=_bearer(_key(keys, name="chat client")))
    assert reply.headers["X-Axiom-User-Name"] == ""


def test_the_scopes_are_reported(client, keys):
    """What a caller should gate on, since a role name is not recoverable."""
    reply = client.get("/gate/verify", headers=_bearer(_key(keys)))
    assert reply.headers["X-Axiom-User-Scopes"] == "chat:invoke,rag:read"


def test_the_roles_header_is_empty_for_a_key(client, keys):
    """Not an oversight. A role is resolved through the bundles when the key is
    issued and only the resulting scopes are stored, so the node cannot say
    afterwards which role a key came from. A guess would be worse than nothing."""
    reply = client.get("/gate/verify", headers=_bearer(_key(keys)))
    assert reply.headers["X-Axiom-User-Roles"] == ""


def test_the_site_is_reported(client, keys):
    reply = client.get("/gate/verify", headers=_bearer(_key(keys, site="example")))
    assert reply.headers["X-Axiom-User-Site"] == "example"


def test_which_family_authenticated_is_reported(client, keys):
    """So a caller cannot mistake a service key for a signed-in person."""
    reply = client.get("/gate/verify", headers=_bearer(_key(keys)))
    assert reply.headers["X-Axiom-Auth"] == "api-key"


def test_the_key_id_is_reported_for_an_incident(client, keys):
    reply = client.get("/gate/verify", headers=_bearer(_key(keys)))
    assert reply.headers["X-Axiom-Key-Id"], "no key id to grep for"


def test_every_contract_header_is_present_even_when_empty(client, keys):
    """A missing header and an empty one read differently to a front door, and
    an absent header is the one that becomes a KeyError at 3am."""
    reply = client.get("/gate/verify", headers=_bearer(_key(keys)))
    for header in (
        "X-Axiom-Auth",
        "X-Axiom-User-Id",
        "X-Axiom-User-Email",
        "X-Axiom-User-Name",
        "X-Axiom-User-Roles",
        "X-Axiom-User-Site",
        "X-Axiom-User-Scopes",
        "X-Axiom-Key-Id",
    ):
        assert header in reply.headers, f"{header} is absent"


# ---------------------------------------------------------------------------
# Refusals, and the one that matters most
# ---------------------------------------------------------------------------


def test_an_unknown_key_is_refused(client, keys):
    _key(keys)
    reply = client.get("/gate/verify", headers=_bearer("axk_000000000000_nope"))
    assert reply.status_code == 401


def test_a_revoked_key_is_refused(client, keys):
    from axiom.webauth.api_keys import load_api_key_records, revoke_api_key_record

    token = _key(keys)
    key_id = load_api_key_records(keys)[0]["key_id"]
    revoke_api_key_record(keys, key_id)
    assert client.get("/gate/verify", headers=_bearer(token)).status_code == 401


def test_a_bearer_never_falls_back_to_a_cookie(client, keys, monkeypatch):
    """The one that would be a vulnerability rather than a bug.

    A caller whose key was revoked must not keep working because they also hold a
    session cookie. Nobody looking at the revoked key would understand why it
    still worked.
    """
    from axiom.extensions.builtins.webgate.api import routers

    monkeypatch.setattr(
        routers, "session_from_cookies", lambda *_a, **_k: {"sub": "someone", "email": "a@b"}
    )
    reply = client.get("/gate/verify", headers=_bearer("axk_000000000000_nope"))
    assert reply.status_code == 401, "an invalid bearer was rescued by a cookie"
    assert "X-Axiom-User-Id" not in reply.headers


def test_a_malformed_authorization_header_is_refused_not_ignored(client, keys):
    """`Authorization: Basic …` is a presented credential of the wrong kind, and
    treating it as absent would fall through to the cookie."""
    _key(keys)
    reply = client.get("/gate/verify", headers={"authorization": "Basic abc"})
    assert reply.status_code == 401


def test_no_credential_at_all_is_still_a_401(client, keys):
    assert client.get("/gate/verify").status_code == 401


# ---------------------------------------------------------------------------
# The session path, unchanged
# ---------------------------------------------------------------------------


def test_a_session_still_verifies_exactly_as_before(client, keys, monkeypatch):
    from axiom.extensions.builtins.webgate.api import routers

    monkeypatch.setattr(
        routers,
        "session_from_cookies",
        lambda *_a, **_k: {
            "sub": "u-1",
            "email": "someone@example.edu",
            "name": "Someone",
            "roles": ["viewer"],
            "site": "example",
        },
    )
    reply = client.get("/gate/verify")
    assert reply.status_code == 200
    assert reply.headers["X-Axiom-User-Id"] == "u-1"
    assert reply.headers["X-Axiom-User-Email"] == "someone@example.edu"
    assert reply.headers["X-Axiom-User-Name"] == "Someone"
    assert reply.headers["X-Axiom-User-Roles"] == "viewer"
    assert reply.headers["X-Axiom-User-Site"] == "example"


def test_a_session_reports_its_family_and_no_scopes(client, keys, monkeypatch):
    """A session carries roles, a key carries scopes. A front door reading the
    wrong one for the wrong family would silently allow or deny everything."""
    from axiom.extensions.builtins.webgate.api import routers

    monkeypatch.setattr(
        routers, "session_from_cookies", lambda *_a, **_k: {"sub": "u-1", "roles": ["viewer"]}
    )
    reply = client.get("/gate/verify")
    assert reply.headers["X-Axiom-Auth"] == "session"
    assert reply.headers["X-Axiom-User-Scopes"] == ""
    assert reply.headers["X-Axiom-Key-Id"] == ""


# ---------------------------------------------------------------------------
# Cheap, because a front door calls this on every request
# ---------------------------------------------------------------------------


def test_the_key_store_is_not_rebuilt_per_request(client, keys):
    """The store caches its parse by mtime and its scrypt verification per key.
    Constructing one per request throws both away, which turns a memory-hard
    hash into per-request work on the hot path of every chat message.
    """
    token = _key(keys)
    from axiom.webauth import api_keys as api_keys_mod

    built = {"n": 0}
    original = api_keys_mod.JsonFileApiKeyStore.__init__

    def counting(self, path):
        built["n"] += 1
        original(self, path)

    api_keys_mod.JsonFileApiKeyStore.__init__ = counting
    try:
        for _ in range(5):
            assert client.get("/gate/verify", headers=_bearer(token)).status_code == 200
    finally:
        api_keys_mod.JsonFileApiKeyStore.__init__ = original
    assert built["n"] <= 1, f"the key store was constructed {built['n']} times for 5 requests"


def test_a_revocation_still_takes_effect_without_a_restart(client, keys):
    """The other side of caching: holding the store must not hold stale records.
    The store re-reads on mtime change, and this proves the route gets that."""
    from axiom.webauth.api_keys import load_api_key_records, revoke_api_key_record

    token = _key(keys)
    assert client.get("/gate/verify", headers=_bearer(token)).status_code == 200
    revoke_api_key_record(keys, load_api_key_records(keys)[0]["key_id"])
    assert client.get("/gate/verify", headers=_bearer(token)).status_code == 401


def test_the_token_is_never_logged(client, keys, caplog):
    import logging

    token = _key(keys)
    with caplog.at_level(logging.DEBUG):
        client.get("/gate/verify", headers=_bearer(token))
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert token.split("_")[-1] not in logged
