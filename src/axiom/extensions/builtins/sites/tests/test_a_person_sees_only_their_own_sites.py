# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tenants and the scope selector, through the served path.

- MVP-10-A1: a person with one tenant sees no other tenant's names, counts or
  objects anywhere.
- MVP-10-A2: adding a tenant is configuration only (no code edit).
- MVP-15-A1: the selection carries across rail items and survives reload (the
  server half: every route narrows to the cookie the selector sets).
- MVP-15-A2: people with one site never see the selector (``show_selector``).

One node serves two tenants. Ana's credential is held to ``site-a``; the operator's
is not held to a site. Each test drives the same composed app the node serves:
the authz middleware, the sites, search and chat routes.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

pytest.importorskip("fastapi")

from fastapi import APIRouter, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from axiom.infra import site_authority as sa  # noqa: E402
from axiom.infra import site_identity, tenants  # noqa: E402

A, B = "site-a", "site-b"

REGISTRY = """
[[tenant]]
id = "site-a"
name = "Site A"

[[tenant]]
id = "site-b"
name = "Site B"
also_known_as = ["site-b-old"]
"""


@pytest.fixture(autouse=True)
def node(tmp_path, monkeypatch):
    path = tmp_path / "tenants.toml"
    path.write_text(REGISTRY, encoding="utf-8")
    monkeypatch.setenv(tenants.TENANTS_FILE_ENV, str(path))
    monkeypatch.delenv("AXIOM_SERVED_SITES", raising=False)
    monkeypatch.delenv("AXIOM_SITE", raising=False)
    monkeypatch.delenv("AXIOM_HOME_SITES", raising=False)
    tenants._reset_for_tests()
    sa._reset_default_site_context()
    monkeypatch.setattr(site_identity, "_IDENTITIES", site_identity.SiteIdentities())
    yield path
    tenants._reset_for_tests()
    sa._reset_default_site_context()


@pytest.fixture
def chat_engine():
    from axiom.extensions.builtins.chat import api as chat_api
    from axiom.extensions.builtins.chat.db_models import Base

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    chat_api.set_test_engine(engine)
    for site in (A, B):
        chat_api._store().create(site_id=site, tenant_id="", principal_id="")
    yield engine
    chat_api.set_test_engine(None)
    engine.dispose()


@pytest.fixture(autouse=True)
def two_site_search(monkeypatch):
    from axiom.extensions.builtins.search import engine

    def hits(q, scope, limit):
        return [engine.Hit(kind="conversation", id=f"c-{s}", title=f"pump {s}", snippet="",
                           url="#", score=0.9, site=s, meta={}) for s in (A, B)]

    monkeypatch.setitem(engine.PROVIDERS, "conversation", hits)


def _app(*, site: str | None) -> TestClient:
    """The served app, as a person whose credential is held to ``site`` (or none)."""
    from axiom.extensions.builtins.authz.decide import decide
    from axiom.extensions.builtins.chat import api as chat_api
    from axiom.extensions.builtins.http.authz_hook import (
        ResolvedCredential,
        _principal_from_handle,
        build_authz_hook,
    )
    from axiom.extensions.builtins.http.middleware import _install_authz
    from axiom.extensions.builtins.search import api as search_api
    from axiom.extensions.builtins.sites import api as sites_api

    handle = f"@ana:{site}" if site else "@operator:node"
    claims = {"sub": handle[1:].split(":")[0], **({"site": site} if site else {})}

    def who(request):
        return ResolvedCredential(principal=_principal_from_handle(handle), credential_id="t",
                                  claims=claims)

    ctx = sa.build_site_context()
    from axiom.extensions.builtins.authz.rules import Rule
    from axiom.governance.intent import IntentPattern
    from axiom.governance.resource import ResourcePattern

    ctx.add_rule(Rule(name="open_http", intent_pattern=IntentPattern("http.*"), actor_pattern="*",
                      resource_pattern=ResourcePattern("extension://*"), disposition="permit"))
    app = FastAPI()
    _install_authz(app, build_authz_hook(resolve_principal=who,
                                         decide_fn=lambda env: decide(env, ctx)), lambda: [])
    router = APIRouter(prefix="/api/v1")
    sites_api.register_site_routes(router)
    search_api.register_routes(router)
    chat_api.register_routes(router, subpath="/chat")
    app.include_router(router)
    return TestClient(app)


def _search_sites(client) -> set[str]:
    r = client.get("/api/v1/search", params={"q": "pump", "kinds": "conversation"})
    assert r.status_code == 200, r.text
    return {h["site"] for h in r.json()["results"]}


def _chat_sites(client) -> list[str]:
    return [c["site_id"] for c in client.get("/api/v1/chat/conversations").json()["conversations"]]


# ---------------------------------------------------------------------------
# MVP-10-A1
# ---------------------------------------------------------------------------


def test_mvp_10_a1_a_one_tenant_person_sees_no_other_tenant_anywhere(chat_engine):
    client = _app(site=A)

    listed = client.get("/api/v1/sites").json()
    assert [s["id"] for s in listed["sites"]] == [A]
    assert B not in str(listed) and "Site B" not in str(listed)

    hidden = client.get(f"/api/v1/sites/{B}")
    absent = client.get("/api/v1/sites/not-hosted-here")
    assert hidden.status_code == absent.status_code == 404
    assert hidden.json()["detail"].replace(B, "X") == \
        absent.json()["detail"].replace("not-hosted-here", "X")

    assert _search_sites(client) == {A}
    assert _chat_sites(client) == [A]


def test_mvp_10_a1_the_selector_cookie_cannot_widen_a_tenant(chat_engine):
    """Naming the other tenant in the cookie shows nothing of it."""
    client = _app(site=A)
    client.cookies.set(sa.SCOPE_COOKIE, B)
    assert _search_sites(client) == set()
    assert _chat_sites(client) == []
    assert [s["id"] for s in client.get("/api/v1/sites").json()["sites"]] == [A]


def test_mvp_10_a1_an_sso_sign_in_keeps_the_tenant_an_administrator_set(tmp_path):
    """An SSO re-sign-in rewrote the account without its site, which left the
    tenant: an unsited account is held to no site."""
    from axiom.webauth import JsonFileUserStore, User, upsert_user_record

    path = tmp_path / "users.json"
    upsert_user_record(path, email="ana@example.org", password_hash=None, roles=["reader"],
                       site=A, attributes={"idp": "campus"})
    store = JsonFileUserStore(path)
    existing = store.get_by_email("ana@example.org")
    store.upsert(User(user_id=existing.user_id, email=existing.email, name="Ana",
                      roles=existing.roles, attributes={**existing.attributes, "idp_subject": "s1"}))
    assert JsonFileUserStore(path).get_by_email("ana@example.org").site == A


# ---------------------------------------------------------------------------
# MVP-10-A2
# ---------------------------------------------------------------------------


def test_mvp_10_a2_adding_a_tenant_is_one_block_in_the_registry(node, chat_engine):
    operator = _app(site=None)
    assert [s["id"] for s in operator.get("/api/v1/sites").json()["sites"]] == [A, B]

    node.write_text(REGISTRY + '\n[[tenant]]\nid = "site-c"\nname = "Site C"\n', encoding="utf-8")

    listed = operator.get("/api/v1/sites").json()["sites"]
    assert [(s["id"], s["name"]) for s in listed] == [(A, "Site A"), (B, "Site B"),
                                                      ("site-c", "Site C")]
    # A person of the new tenant is held to it, by configuration alone.
    assert [s["id"] for s in _app(site="site-c").get("/api/v1/sites").json()["sites"]] == ["site-c"]


def test_mvp_10_a2_a_former_id_in_the_registry_is_the_same_tenant(node):
    tenants.load()
    assert site_identity.resolve("site-b-old") == B
    assert _app(site=None).get("/api/v1/sites/site-b-old").json()["id"] == B


def test_mvp_10_a2_a_broken_registry_serves_no_extra_tenant_and_says_why(node, caplog):
    node.write_text("[[tenant]]\nname = 'no id'\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert tenants.ids() == frozenset()
    assert "'id' is required" in caplog.text


# ---------------------------------------------------------------------------
# MVP-15-A1 / A2 (the server half; the header itself is appkit's)
# ---------------------------------------------------------------------------


def test_mvp_15_a1_the_choice_narrows_every_route_and_survives_a_new_page_load(chat_engine):
    first = _app(site=None)
    first.cookies.set(sa.SCOPE_COOKIE, B)
    assert first.get("/api/v1/sites").json()["selected"] == [B]
    assert _search_sites(first) == {B}          # one rail item
    assert _chat_sites(first) == [B]            # another

    reloaded = _app(site=None)                  # a reload: a new page, the same cookie
    reloaded.cookies.set(sa.SCOPE_COOKIE, B)
    assert _search_sites(reloaded) == {B}

    reloaded.cookies.set(sa.SCOPE_COOKIE, "*")  # "all sites"
    assert _search_sites(reloaded) == {A, B}


def test_mvp_15_a1_the_choice_is_honoured_over_mcp_through_the_same_scope(monkeypatch):
    from axiom.extensions.builtins.mcp.skill_tools import skill_tool_contribution
    from axiom.extensions.builtins.search.skills import bind
    from axiom.infra.skills import SkillContext, SkillRegistry

    reg = SkillRegistry()
    bind(reg)
    tools = skill_tool_contribution(reg, ctx_factory=lambda: SkillContext(
        registry=reg, state_dir=__import__("tempfile").mkdtemp(), logger=logging.getLogger("t")))
    token = sa.bind_selected_sites(frozenset({A}))
    try:
        found = asyncio.run(tools.dispatch["axiom_search__query"](
            {"query": "pump", "kinds": ["conversation"]}))
    finally:
        sa.unbind_selected_sites(token)
    assert {h["site"] for h in found["value"]["results"]} == {A}


def test_mvp_15_a2_a_person_with_one_site_is_not_offered_the_selector():
    assert _app(site=A).get("/api/v1/sites").json()["show_selector"] is False
    assert _app(site=None).get("/api/v1/sites").json()["show_selector"] is True
