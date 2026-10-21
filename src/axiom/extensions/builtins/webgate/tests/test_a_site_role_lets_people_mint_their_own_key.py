# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A site defines the role its people sign in with, and they mint their own key on a page.

Two gaps stood between a signed-in person and their key on 2026-10-06:

* ``load_overrides`` let a deployment define a role inside what the built-in
  roles grant, and nothing ever called it, so a site could not say "people
  who sign in may chat and read" without a code change; minting refused any
  role the built-ins did not name.
* ``POST /gate/keys`` minted a key for a signed-in person, and no page offered
  it, so in practice nobody could: a browser session cannot call it by hand.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from axiom.extensions.builtins.webgate import role_bundles  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch):
    monkeypatch.setattr(role_bundles, "_default", None)
    yield
    role_bundles._default = None


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
    return TestClient(app, follow_redirects=False)


def _signed_in(monkeypatch, **claims):
    from axiom.extensions.builtins.webgate.api import routers

    base = {
        "sub": "u-1",
        "email": "someone@example.edu",
        "name": "Someone",
        "roles": ["viewer"],
        "site": "example",
    }
    base.update(claims)
    monkeypatch.setattr(routers, "session_from_cookies", lambda *_a, **_k: base)
    return base


def _roles_file(tmp_path, monkeypatch, text: str) -> Path:
    path = tmp_path / "roles.toml"
    path.write_text(text)
    monkeypatch.setenv(role_bundles.ROLES_FILE_ENV, str(path))
    return path


# -- a site's role --------------------------------------------------------------


def test_a_role_the_site_defines_is_minted_with_its_scopes(client, keys, monkeypatch, tmp_path):
    _roles_file(tmp_path, monkeypatch, '[role.reviewer]\nscopes = ["chat:invoke", "*:read"]\n')
    _signed_in(monkeypatch, roles=["reviewer"])
    body = client.post("/gate/keys", json={}).json()
    assert sorted(body["scopes"]) == ["*:read", "chat:invoke"]


def test_a_site_role_cannot_grant_more_than_the_built_ins_can(monkeypatch, tmp_path):
    _roles_file(
        tmp_path,
        monkeypatch,
        '[role.too_much]\nscopes = ["*"]\n[role.viewer]\nscopes = ["*:invoke"]\n',
    )
    with pytest.raises(role_bundles.OverrideWidensError):
        role_bundles.default_bundle_registry()


def test_a_named_roles_file_that_is_missing_is_an_error_not_silence(monkeypatch, tmp_path):
    monkeypatch.setenv(role_bundles.ROLES_FILE_ENV, str(tmp_path / "absent.toml"))
    with pytest.raises(FileNotFoundError):
        role_bundles.default_bundle_registry()


def test_without_a_roles_file_the_built_ins_are_unchanged(monkeypatch):
    monkeypatch.delenv(role_bundles.ROLES_FILE_ENV, raising=False)
    assert set(role_bundles.default_bundle_registry().roles()) >= {"owner", "admin", "viewer"}


# -- the page ---------------------------------------------------------------------


def test_the_page_sends_someone_signed_out_to_sign_in_and_back(client, monkeypatch):
    from axiom.extensions.builtins.webgate.api import routers

    monkeypatch.setattr(routers, "session_from_cookies", lambda *_a, **_k: None)
    reply = client.get("/gate/keys")
    assert reply.status_code in (302, 303, 307)
    assert "/gate/login" in reply.headers["location"] and "keys" in reply.headers["location"]


def test_the_page_offers_the_key_and_says_what_it_will_allow(client, monkeypatch, tmp_path):
    _roles_file(tmp_path, monkeypatch, '[role.reviewer]\nscopes = ["chat:invoke", "*:read"]\n')
    _signed_in(monkeypatch, roles=["reviewer"])
    page = client.get("/gate/keys")
    assert page.status_code == 200
    text = page.text
    assert "Create a key" in text and "chat:invoke" in text
    assert "secrets set" in text  # how to store it, shown with the key
    assert "axk_" not in text  # no key exists until the person asks for one


def test_the_page_says_plainly_when_there_is_no_role(client, monkeypatch):
    _signed_in(monkeypatch, roles=[])
    page = client.get("/gate/keys")
    assert page.status_code == 200 and "no role" in page.text.lower()
    assert "Create a key" not in page.text
