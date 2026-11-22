# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Account › Keys: a person's own keys, rescoped in place without re-authenticating.

The backend already kept a token across ``gate.rescope`` for an administrator.
Account › Keys gives the holder the same move on their own keys:

- ``GET /gate/keys/mine`` lists only the caller's keys, never a hash or a token;
- ``POST /gate/keys/mine/{id}/rescope`` narrows a key (or restores a scope the
  caller's roles grant) and the same token keeps authenticating (MVP-08-A5);
- nobody rescopes somebody else's key, and nobody widens theirs past their roles;
- each rescope is one audit row with old → new, as the caller.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from axiom.infra import audit_trail  # noqa: E402


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AXIOM_AUTHORITY_RECEIPTS", "off")
    monkeypatch.setenv("AXIOM_SITE", "example")
    audit_trail._reset_for_tests()
    yield
    audit_trail._reset_for_tests()


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
    from axiom.extensions.builtins.webgate.api import routers

    base = {"sub": "u-1", "email": "someone@example.edu", "name": "Someone",
            "roles": ["creator"], "site": "example"}
    base.update(claims)
    monkeypatch.setattr(routers, "session_from_cookies", lambda *_a, **_k: base)
    return base


def _resolve(keys: Path, token: str):
    from axiom.webauth.api_keys import JsonFileApiKeyStore

    return JsonFileApiKeyStore(keys).resolve(token)


@pytest.mark.acceptance("MVP-08-A5")
def test_rescoping_my_key_from_account_keeps_the_same_token_working(client, keys, monkeypatch):
    _signed_in(monkeypatch)
    minted = client.post("/gate/keys", json={}).json()
    token, key_id = minted["token"], minted["key_id"]
    scopes = list(minted["scopes"])
    assert len(scopes) >= 2, "the creator role grants more than one scope to narrow"

    listed = client.get("/gate/keys/mine").json()
    assert [k["key_id"] for k in listed["keys"]] == [key_id]
    assert all("secret_hash" not in k and "token" not in k for k in listed["keys"])

    drop = scopes[-1]
    reply = client.post(f"/gate/keys/mine/{key_id}/rescope", json={"remove_scope": [drop]})
    assert reply.status_code == 200, reply.text
    assert reply.json()["same_token"] is True
    assert drop not in reply.json()["key"]["scopes"]

    # The same token still authenticates, held to the narrower scopes.
    principal = _resolve(keys, token)
    assert principal is not None and drop not in set(principal.scopes)

    # And it can be restored, because the role grants it: still the same token.
    back = client.post(f"/gate/keys/mine/{key_id}/rescope", json={"add_scope": [drop]})
    assert back.status_code == 200, back.text
    assert drop in set(_resolve(keys, token).scopes)


@pytest.mark.acceptance("MVP-08-A5")
def test_nobody_rescopes_another_persons_key_or_widens_their_own(client, keys, monkeypatch):
    _signed_in(monkeypatch, sub="u-2", email="other@example.edu")
    theirs = client.post("/gate/keys", json={}).json()["key_id"]

    _signed_in(monkeypatch)
    mine = client.post("/gate/keys", json={}).json()["key_id"]
    assert client.post(f"/gate/keys/mine/{theirs}/rescope", json={"remove_scope": ["x"]}).status_code == 404
    assert client.post(f"/gate/keys/mine/{theirs}/revoke").status_code == 404
    widened = client.post(f"/gate/keys/mine/{mine}/rescope", json={"add_scope": ["admin:write"]})
    assert widened.status_code == 403 and "admin:write" in widened.json()["detail"]


@pytest.mark.acceptance("MVP-08-A5")
def test_a_rescope_from_account_is_one_audit_row_with_old_and_new(client, keys, monkeypatch, tmp_path):
    _signed_in(monkeypatch)
    minted = client.post("/gate/keys", json={}).json()
    before = len(audit_trail.read_chain(audit_trail.audit_path(tmp_path / "state")))
    client.post(f"/gate/keys/mine/{minted['key_id']}/rescope", json={"remove_scope": [minted["scopes"][-1]]})
    rows = audit_trail.read_chain(audit_trail.audit_path(tmp_path / "state"))[before:]
    assert [r["skill"] for r in rows] == ["gate.rescope"]
    change = rows[0]["change"]
    assert change["kind"] == "api-key" and minted["scopes"][-1] in change["old"]["scopes"]
    assert minted["scopes"][-1] not in change["new"]["scopes"]


def test_revoking_my_key_from_account_ends_it(client, keys, monkeypatch):
    _signed_in(monkeypatch)
    minted = client.post("/gate/keys", json={}).json()
    assert client.post(f"/gate/keys/mine/{minted['key_id']}/revoke").status_code == 200
    assert _resolve(keys, minted["token"]) is None
    assert client.get("/gate/keys/mine").json()["keys"] == []
