# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A key minted at /gate/keys always carries a site, and only one the account may use.

Seen live: two self-service keys came out with ``site = None``, because the mint
took the site from the session and a sign-in that names no site gave it none. A
data kit's tenant is the key's site, and every per-site read checks it, so such a
key could read nothing site-held and promote nothing.

- the node's home site (``AXIOM_SITE``) is the default;
- a role may list more sites (``sites = [...]`` in the roles file), for a guest
  site the node hosts, and the page offers exactly those;
- any other site is refused in words;
- a key minted before this keeps working, with one warning per key, and
  ``gate list api-keys`` flags it.
"""

from __future__ import annotations

import json
import logging

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def keys(tmp_path, monkeypatch):
    path = tmp_path / "api-keys.json"
    monkeypatch.setenv("AXIOM_GATE_API_KEYS_FILE", str(path))
    monkeypatch.setenv("AXIOM_SITE", "home-node")
    return path


@pytest.fixture
def roles(tmp_path, monkeypatch):
    from axiom.extensions.builtins.webgate import role_bundles

    f = tmp_path / "roles.toml"
    f.write_text('[role.contributor]\nscopes = ["*:read"]\nsites = ["guest-loop"]\n')
    monkeypatch.setenv(role_bundles.ROLES_FILE_ENV, str(f))
    monkeypatch.setattr(role_bundles, "_default", None)
    yield
    role_bundles._default = None


@pytest.fixture
def client(keys):
    from axiom.extensions.builtins.webgate.api.routers import build_webgate_router

    app = FastAPI()
    app.include_router(build_webgate_router(oidc=None, secure_cookies=False))
    return TestClient(app)


def _signed_in(monkeypatch, **claims):
    from axiom.extensions.builtins.webgate.api import routers

    base = {"sub": "u-1", "email": "someone@example.edu", "name": "Someone", "roles": ["viewer"]}
    base.update(claims)
    monkeypatch.setattr(routers, "session_from_cookies", lambda *_a, **_k: base)
    return base


def test_a_session_naming_no_site_still_gets_a_key_for_the_home_site(client, keys, monkeypatch):
    _signed_in(monkeypatch)  # no "site" claim, as the live sign-in had
    body = client.post("/gate/keys", json={}).json()
    assert body["site"] == "home-node", body
    stored = json.loads(keys.read_text())
    stored = stored if isinstance(stored, list) else stored.get("keys", stored)
    assert all(r.get("site") == "home-node" for r in stored), stored


def test_a_role_that_lists_a_guest_site_may_mint_for_it_and_the_page_offers_it(client, roles, monkeypatch):
    _signed_in(monkeypatch, roles=["contributor"])
    page = client.get("/gate/keys").text
    assert '<option value="home-node">' in page and '<option value="guest-loop">' in page
    body = client.post("/gate/keys", json={"site": "guest-loop"}).json()
    assert body["site"] == "guest-loop" and body["principal"].endswith(":guest-loop"), body


def test_a_site_the_account_may_not_use_is_refused_in_words(client, roles, monkeypatch):
    _signed_in(monkeypatch, roles=["contributor"])
    reply = client.post("/gate/keys", json={"site": "someone-elses-site"})
    assert reply.status_code == 403
    assert reply.json()["code"] == "site_not_allowed"
    assert "home-node, guest-loop" in reply.json()["detail"]
    _signed_in(monkeypatch, roles=["viewer"])  # a role that lists no guest site
    assert client.post("/gate/keys", json={"site": "guest-loop"}).status_code == 403


def test_an_unsited_key_still_works_warns_once_and_is_flagged_in_the_list(tmp_path, monkeypatch, caplog):
    from axiom.extensions.builtins.webgate.skills.list_users import _list_api_keys
    from axiom.webauth.api_keys import JsonFileApiKeyStore, append_api_key_record, mint_api_key

    path = tmp_path / "keys.json"
    token, record = mint_api_key(principal="@old", scopes=["*:read"], name="from before")
    assert record["site"] is None
    append_api_key_record(path, record)
    store = JsonFileApiKeyStore(path)
    with caplog.at_level(logging.WARNING, logger="axiom.webauth.api_keys"):
        assert store.resolve(token) is not None
        assert store.resolve(token) is not None
    warned = [r for r in caplog.records if "carries no site" in r.getMessage()]
    assert len(warned) == 1, [r.getMessage() for r in caplog.records]

    listed = _list_api_keys({"keys_file": str(path)})
    assert listed.ok and listed.value["unsited"] == [record["key_id"]]
    assert any("carries no site" in a for a in listed.actions_taken)
