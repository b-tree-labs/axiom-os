# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The site reaches the gate's surfaces: ``/gate/verify`` emits it, ``/gate/me``
returns it, and ``axi gate adduser|issue --site`` bind accounts and keys to it."""

from __future__ import annotations

import logging
import warnings
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.webgate import skills as gate_skills
from axiom.extensions.builtins.webgate.api.routers import build_webgate_router
from axiom.infra.skills import SkillContext, SkillRegistry
from axiom.webauth import JsonFileUserStore, get_password_hash
from axiom.webauth.api_keys import JsonFileApiKeyStore
from axiom.webauth.keys import reset_key_store_for_tests
from axiom.webauth.users import InMemoryUserStore, User

warnings.filterwarnings("ignore")
PW = "correct-horse-battery-staple"


@pytest.fixture(autouse=True)
def _keys():
    reset_key_store_for_tests()
    yield
    reset_key_store_for_tests()


@pytest.fixture
def ctx(tmp_path: Path) -> SkillContext:
    reg = SkillRegistry()
    gate_skills.bind(reg)
    return SkillContext(registry=reg, state_dir=tmp_path, logger=logging.getLogger("test.gate"))


def _client(store):
    app = FastAPI()
    app.include_router(
        build_webgate_router(store, secure_cookies=False, oidc=None)
        if "oidc" in build_webgate_router.__code__.co_varnames
        else build_webgate_router(store, secure_cookies=False)
    )
    return TestClient(app, base_url="http://gate.example", follow_redirects=False)


def test_verify_and_me_carry_the_site():
    store = InMemoryUserStore(
        [
            User(
                user_id="u1",
                email="alice@example.edu",
                password_hash=get_password_hash(PW),
                roles=("operator",),
                site="ut-triga",
            )
        ]
    )
    c = _client(store)
    r = c.post("/gate/login", data={"email": "alice@example.edu", "password": PW, "next": "/"})
    assert r.status_code == 303
    v = c.get("/gate/verify")
    assert v.status_code == 200
    assert v.headers["X-Axiom-User-Site"] == "ut-triga"
    assert c.get("/gate/me").json()["site"] == "ut-triga"


def test_verify_emits_an_empty_site_header_for_siteless_accounts():
    store = InMemoryUserStore(
        [User(user_id="u1", email="a@x.org", password_hash=get_password_hash(PW))]
    )
    c = _client(store)
    c.post("/gate/login", data={"email": "a@x.org", "password": PW, "next": "/"})
    v = c.get("/gate/verify")
    assert v.headers["X-Axiom-User-Site"] == ""
    assert c.get("/gate/me").json()["site"] is None


def test_adduser_with_site_binds_the_account(ctx: SkillContext, tmp_path: Path, monkeypatch):
    accounts = tmp_path / "accounts.json"
    r = ctx.registry.invoke(
        "gate.adduser",
        {
            "email": "acu-user@acu.edu",
            "password": "Correct-Horse-Battery-1",
            "accounts_file": str(accounts),
            "role": ["student"],
            "site": "acu",
        },
        ctx,
    )
    assert r.ok, r.errors
    assert r.value["site"] == "acu"
    assert JsonFileUserStore(accounts).get_by_email("acu-user@acu.edu").site == "acu"
    # the env default applies when --site is omitted
    monkeypatch.setenv("AXIOM_SITE", "ut-triga")
    r = ctx.registry.invoke(
        "gate.adduser",
        {
            "email": "b@example.edu",
            "password": "Correct-Horse-Battery-1",
            "accounts_file": str(accounts),
        },
        ctx,
    )
    assert r.ok and r.value["site"] == "ut-triga"
    r = ctx.registry.invoke(
        "gate.adduser",
        {
            "email": "c@example.edu",
            "password": "Correct-Horse-Battery-1",
            "accounts_file": str(accounts),
            "site": "not a site",
        },
        ctx,
    )
    assert not r.ok and "invalid site" in r.errors[0]
    listing = ctx.registry.invoke(
        "gate.list", {"resource": "accounts", "accounts_file": str(accounts)}, ctx
    )
    assert {i["email"]: i["site"] for i in listing.value["items"]} == {
        "acu-user@acu.edu": "acu",
        "b@example.edu": "ut-triga",
    }


def test_issue_with_site_binds_the_key_and_refuses_a_mismatch(ctx: SkillContext, tmp_path: Path):
    keys = tmp_path / "keys.json"
    r = ctx.registry.invoke(
        "gate.issue",
        {
            "resource": "api-key",
            "principal": "@ingest",
            "scope": ["ingest"],
            "keys_file": str(keys),
            "site": "acu",
        },
        ctx,
    )
    assert r.ok, r.errors
    assert r.value["principal"] == "@ingest:acu" and r.value["site"] == "acu"
    identity = JsonFileApiKeyStore(keys).resolve(r.value["token"])
    assert identity.site == "acu"
    bad = ctx.registry.invoke(
        "gate.issue",
        {
            "resource": "api-key",
            "principal": "@ingest:vcu",
            "scope": ["ingest"],
            "keys_file": str(keys),
            "site": "acu",
        },
        ctx,
    )
    assert not bad.ok and "names site" in bad.errors[0]
    listing = ctx.registry.invoke(
        "gate.list", {"resource": "api-keys", "keys_file": str(keys)}, ctx
    )
    assert listing.value["items"][0]["site"] == "acu"
