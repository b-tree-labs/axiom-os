# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""MVP-07-A1: one decision function answers for page, API, search, chat and MCP.

Pages and APIs were decided by ``decide()`` through the authz hook, and a read
that named a site was decided by it too. Search and chat read through
``site_scope``, which applied the credential's site and nothing else, so a rule
an operator wrote for a site held in a telemetry query and not in a search result
or a chat list. Now ``site_scope.resolve`` asks the same ``decide()`` per site.

One policy is written here, once: a page, an API route and a site are denied.
Each of the five surfaces is then driven, and each must both honour the rule and
have reached it through the one function (a spy on ``decide`` records who asked).
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

import importlib  # noqa: E402

# The package re-exports the function under the module's name, so the module is
# fetched by path: the spy must replace what every caller looks up.
decide_mod = importlib.import_module("axiom.extensions.builtins.authz.decide")
from axiom.extensions.builtins.authz.rules import Rule  # noqa: E402
from axiom.governance.intent import IntentPattern  # noqa: E402
from axiom.governance.resource import ResourcePattern  # noqa: E402
from axiom.infra import site_authority as sa  # noqa: E402

SITE_A, SITE_B = "site-a", "site-b"


def _rule(name: str, intent: str, resource: str, disposition: str, priority: int = 0) -> Rule:
    return Rule(name=name, intent_pattern=IntentPattern(intent), actor_pattern="*",
                resource_pattern=ResourcePattern(resource), disposition=disposition,
                priority=priority)


@pytest.fixture
def policy(monkeypatch):
    """The one context every surface decides against, and a spy on ``decide``."""
    ctx = sa.build_site_context()
    ctx.add_rule(_rule("open_http", "http.*", "extension://*", "permit", priority=-1000))
    ctx.add_rule(_rule("deny_secret_page", "http.*", "extension://http/app/secret", "deny"))
    ctx.add_rule(_rule("deny_secret_api", "http.*", "extension://http/api/v1/secret", "deny"))
    ctx.add_rule(sa.deny_site_rule(SITE_B))
    monkeypatch.setattr(sa, "default_site_context", lambda: ctx)

    calls: list[str] = []
    real = decide_mod.decide

    def spy(envelope, c):
        calls.append(f"{envelope.intent.value} {envelope.resource}")
        return real(envelope, c)

    monkeypatch.setattr(decide_mod, "decide", spy)
    monkeypatch.delenv("AXIOM_SERVED_SITES", raising=False)
    monkeypatch.delenv("AXIOM_TENANTS_FILE", raising=False)
    monkeypatch.delenv("AXIOM_SITE", raising=False)
    return ctx, calls


@pytest.fixture
def chat_engine():
    from axiom.extensions.builtins.chat import api as chat_api
    from axiom.extensions.builtins.chat.db_models import Base

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    chat_api.set_test_engine(engine)
    yield engine
    chat_api.set_test_engine(None)
    engine.dispose()


def _hit(site: str):
    from axiom.extensions.builtins.search.engine import Hit

    return Hit(kind="conversation", id=f"c-{site}", title=f"pump notes {site}", snippet="",
               url="#", score=0.9, site=site, meta={})


@pytest.fixture
def two_site_search(monkeypatch):
    from axiom.extensions.builtins.search import engine

    monkeypatch.setitem(engine.PROVIDERS, "conversation",
                        lambda q, scope, limit: [_hit(SITE_A), _hit(SITE_B)])


def _app(ctx) -> TestClient:
    from axiom.extensions.builtins.chat import api as chat_api
    from axiom.extensions.builtins.http.authz_hook import (
        ResolvedCredential,
        _principal_from_handle,
        build_authz_hook,
    )
    from axiom.extensions.builtins.http.middleware import _install_authz
    from axiom.extensions.builtins.search import api as search_api

    def who(request):
        return ResolvedCredential(principal=_principal_from_handle("@ana:site-a"), scopes=(),
                                  credential_id="session:ana", claims={"sub": "ana"})

    # Wired exactly as `maybe_default_authz_hook` wires production: the hook's
    # decision is `decide(envelope, ctx)`, looked up on the module each call.
    hook = build_authz_hook(resolve_principal=who,
                            decide_fn=lambda env: decide_mod.decide(env, ctx))
    app = FastAPI()
    _install_authz(app, hook, lambda: [])

    @app.get("/app/secret")
    def secret_page():
        return {"page": "secret"}

    @app.get("/app/home")
    def home_page():
        return {"page": "home"}

    @app.get("/api/v1/secret")
    def secret_api():
        return {"ok": True}

    router = APIRouter(prefix="/api/v1")
    search_api.register_routes(router)
    chat_api.register_routes(router, subpath="/chat")
    app.include_router(router)
    return TestClient(app)


def _mcp(tool: str, args: dict) -> dict:
    from axiom.extensions.builtins.mcp.skill_tools import skill_tool_contribution
    from axiom.extensions.builtins.search.skills import bind as bind_search
    from axiom.extensions.builtins.sites.skills import bind as bind_sites
    from axiom.infra.skills import SkillContext, SkillRegistry

    reg = SkillRegistry()
    bind_search(reg)
    bind_sites(reg)
    contrib = skill_tool_contribution(
        reg, ctx_factory=lambda: SkillContext(registry=reg, state_dir=__import__("tempfile").mkdtemp(),
                                              logger=logging.getLogger("t")))
    return asyncio.run(contrib.dispatch[tool](args))


def test_mvp_07_a1_one_decision_function_answers_for_page_api_search_chat_and_mcp(
        policy, chat_engine, two_site_search, monkeypatch):
    ctx, calls = policy
    client = _app(ctx)

    # Page: the rule for one page holds; its neighbour is served.
    assert client.get("/app/secret").status_code == 403
    assert client.get("/app/home").status_code == 200
    assert any("extension://http/app/secret" in c for c in calls)

    # API: the same.
    calls.clear()
    assert client.get("/api/v1/secret").status_code == 403
    assert any("extension://http/api/v1/secret" in c for c in calls)

    # Search: the denied site's hit is dropped, and the drop was decided.
    calls.clear()
    r = client.get("/api/v1/search", params={"q": "pump", "kinds": "conversation"})
    assert r.status_code == 200, r.text
    assert {h["site"] for h in r.json()["results"]} == {SITE_A}
    assert any(c == f"data.read site://{SITE_B}" for c in calls)

    # Chat: a conversation at the denied site is not listed.
    from axiom.extensions.builtins.chat import api as chat_api

    for site in (SITE_A, SITE_B):
        chat_api._store().create(site_id=site, tenant_id="", principal_id="")
    calls.clear()
    listed = client.get("/api/v1/chat/conversations").json()["conversations"]
    assert [c["site_id"] for c in listed] == [SITE_A]
    assert any(c == f"data.read site://{SITE_B}" for c in calls)

    # MCP: the same search and the site list, through the tool dispatch.
    calls.clear()
    found = _mcp("axiom_search__query", {"query": "pump", "kinds": ["conversation"]})
    assert found["ok"], found
    assert {h["site"] for h in found["value"]["results"]} == {SITE_A}
    monkeypatch.setenv("AXIOM_SERVED_SITES", f"{SITE_A},{SITE_B}")
    sites = _mcp("axiom_sites__list", {})
    assert [s["id"] for s in sites["value"]["sites"]] == [SITE_A]
    assert any(c == f"data.read site://{SITE_B}" for c in calls)


def test_mvp_07_a1_removing_the_rule_is_the_only_change_needed_to_show_the_site(
        policy, two_site_search):
    """The rule is the policy: drop it and every surface shows the site again."""
    ctx, _calls = policy
    ctx.rule_engine.rules = [r for r in ctx.rule_engine.rules
                              if not r.name.startswith(sa.DENY_SITE_RULE_PREFIX)]
    found = _mcp("axiom_search__query", {"query": "pump", "kinds": ["conversation"]})
    assert {h["site"] for h in found["value"]["results"]} == {SITE_A, SITE_B}


def test_mvp_07_a1_a_decision_that_cannot_be_made_hides_the_site(policy, monkeypatch,
                                                                  two_site_search):
    """Fail closed: an error deciding a site is a no for that site, never a yes."""
    def broken(envelope, c):
        raise RuntimeError("engine down")

    monkeypatch.setattr(decide_mod, "decide", broken)
    found = _mcp("axiom_search__query", {"query": "pump", "kinds": ["conversation"]})
    assert found["value"]["results"] == []
