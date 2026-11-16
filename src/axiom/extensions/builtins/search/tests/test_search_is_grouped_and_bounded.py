# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The grouped search modal's contract, and its two bounds (MVP-01-A1, MVP-01-A3).

The palette groups results "By name" and "In contents", so every row says which
(``matched``). Every provider reads through the owning extension's own
permission-checked service, so a row the asker may not read never appears, and
``counts`` is computed from the rows returned, so it is never counted either
(MVP-01-A1). The scope selector's site only narrows what the request may already
see; a site outside that set is an empty answer (MVP-01-A3).
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.chat.db_models import Base as ChatBase
from axiom.extensions.builtins.library.tests.test_library_upload import library  # noqa: F401
from axiom.extensions.builtins.search import api, engine
from axiom.infra.orchestrator.session_db import DatabaseSessionStore

ALICE = "@alice:s1"
BOB = "@bob:s1"


def _scope(principal=ALICE, sites=("s1",)):
    return engine.Scope(principal=principal, sites=frozenset(sites) if sites is not None else None)


# -- conversations: the chat store's own ownership clause ---------------------------


@pytest.fixture
def chats(monkeypatch):
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    ChatBase.metadata.create_all(eng)
    store = DatabaseSessionStore(engine=eng)
    monkeypatch.setattr(
        "axiom.infra.orchestrator.conversation_store.resolve_conversation_store",
        lambda engine=None: store,
    )

    def add(principal, title, text, site="s1"):
        session = store.create(site_id=site, principal_id=principal)
        session.title = title
        session.add_message("user", text)
        store.save(session)
        store.rename(session.session_id, title)
        return session.session_id

    yield add
    eng.dispose()


@pytest.mark.acceptance("MVP-01-A1")
class TestConversationsMVP01A1:
    def test_mvp_01_a1_a_title_hit_is_by_name_and_a_message_hit_is_in_contents(self, chats):
        by_name = chats(ALICE, "Reactivity notes", "nothing here")
        in_text = chats(ALICE, "Morning", "the reactivity coefficient drifted")
        out = engine.search("reactivity", _scope(), kinds=["conversation"])
        matched = {r["id"]: r["matched"] for r in out["results"]}
        assert matched == {by_name: "name", in_text: "contents"}
        # Back-compat: the store's own word stays in meta.
        assert {r["meta"]["matched"] for r in out["results"]} == {"title", "message"}

    def test_mvp_01_a1_another_persons_conversation_never_appears_and_is_not_counted(self, chats):
        mine = chats(ALICE, "Reactivity notes", "x")
        chats(BOB, "Reactivity, Bob's", "the reactivity of bob")
        out = engine.search("reactivity", _scope(), kinds=["conversation"])
        assert [r["id"] for r in out["results"]] == [mine]
        assert out["counts"] == {"kind": {"conversation": 1}, "matched": {"name": 1, "contents": 0}}

    def test_mvp_01_a1_a_conversation_on_another_site_never_appears(self, chats):
        chats(ALICE, "Reactivity elsewhere", "x", site="s2")
        out = engine.search("reactivity", _scope(), kinds=["conversation"])
        assert out["results"] == [] and out["counts"]["kind"] == {}


# -- the engine's backstop and counts ------------------------------------------------


def _provider(*hits):
    return lambda q, scope, limit: list(hits)


@pytest.mark.acceptance("MVP-01-A1")
class TestCountsMVP01A1:
    def test_mvp_01_a1_a_hit_on_a_site_outside_the_scope_is_dropped_before_counting(self):
        leak = engine.Hit(kind="library", id="L", title="rod", site="s2", matched="name")
        ok = engine.Hit(kind="library", id="K", title="rod", site="s1", matched="name")
        node = engine.Hit(kind="setting", id="rod.speed", title="rod.speed", matched="name")
        out = engine.search(
            "rod",
            _scope(),
            providers={"library": _provider(leak, ok), "setting": _provider(node)},
            kinds=["library", "setting"],
        )
        assert {r["id"] for r in out["results"]} == {"K", "rod.speed"}
        assert out["counts"] == {
            "kind": {"library": 1, "setting": 1},
            "matched": {"name": 2, "contents": 0},
        }

    def test_mvp_01_a1_every_row_carries_the_contract_fields(self):
        hit = engine.Hit(kind="chart", id="c1", title="rod", url="#/chart/c1", matched="name")
        out = engine.search("rod", _scope(), providers={"chart": _provider(hit)}, kinds=["chart"])
        row = out["results"][0]
        for key in ("kind", "id", "title", "snippet", "url", "matched", "site", "meta"):
            assert key in row
        assert row["matched"] in engine.MATCHED

    def test_mvp_01_a1_the_palette_asks_the_fast_providers_without_their_content_pass(self):
        seen = {}

        def spy(kind):
            def provider(q, scope, limit):
                seen[kind] = scope.contents
                return []

            return provider

        engine.search(
            "rod",
            _scope(),
            mode=engine.SUGGEST,
            providers={k: spy(k) for k in engine.KINDS},
        )
        assert set(seen) == {"conversation", "logbook", "library", "chart", "person"}
        assert not any(seen.values())


# -- library: the library service's own can_read ------------------------------------


@pytest.mark.acceptance("MVP-01-A1")
class TestLibraryMVP01A1:
    def test_mvp_01_a1_another_persons_private_item_never_appears(self, library):  # noqa: F811
        from axiom.extensions.builtins.library import service
        from axiom.extensions.builtins.library.scope import make_caller

        alice, bob = make_caller(ALICE, "alpha"), make_caller(BOB, "alpha")
        shared = service.upload(alice, "pump-notes.txt", b"the pump curve", visibility="site")
        theirs = service.upload(bob, "pump-bob.txt", b"bob's pump", visibility="private")
        mine = service.upload(alice, "pump-mine.txt", b"my pump", visibility="private")
        ids = lambda r: {x["id"] for x in r["results"]}  # noqa: E731

        out = engine.search("pump", _scope(sites=("alpha",)), kinds=["library"])
        assert ids(out) == {shared.item["id"], mine.item["id"]}
        assert theirs.item["id"] not in ids(out)
        assert out["counts"]["kind"] == {"library": 2}
        assert {r["matched"] for r in out["results"]} == {"name"}
        assert all(r["url"] == f"#/library/{r['id']}" for r in out["results"])

        # Bob sees his own and the shared one, never Alice's private one.
        out = engine.search("pump", _scope(BOB, sites=("alpha",)), kinds=["library"])
        assert ids(out) == {shared.item["id"], theirs.item["id"]}

    def test_mvp_01_a1_an_item_on_a_site_outside_the_scope_never_appears(self, library):  # noqa: F811
        from axiom.extensions.builtins.library import service
        from axiom.extensions.builtins.library.scope import make_caller

        service.upload(make_caller(ALICE, "beta"), "pump.txt", b"x", visibility="site")
        out = engine.search("pump", _scope(sites=("alpha",)), kinds=["library"])
        assert out["results"] == []

    def test_mvp_01_a1_nobody_asking_is_skipped_not_answered(self, library):  # noqa: F811
        out = engine.search("pump", _scope(principal=""), kinds=["library"])
        assert out["results"] == [] and "library" in out["skipped"]


# -- charts: the saved views store, per principal ------------------------------------


@pytest.mark.acceptance("MVP-01-A1")
class TestChartsMVP01A1:
    def test_mvp_01_a1_only_the_askers_own_saved_charts(self, monkeypatch):
        docs = {
            ALICE: {
                "groups": [
                    {
                        "name": "Pumps",
                        "graphs": [{"id": "g1", "spec": {"title": "Pump flow", "kind": "line"}}],
                    }
                ]
            },
            BOB: {
                "groups": [
                    {
                        "name": "Bob",
                        "graphs": [{"id": "g2", "spec": {"title": "Pump, Bob's", "kind": "bar"}}],
                    }
                ]
            },
        }
        monkeypatch.setattr(
            "axiom.extensions.builtins.webapp.views.read_view",
            lambda principal, surface, name="last": docs.get(principal),
        )
        out = engine.search("pump", _scope(), kinds=["chart"])
        assert [r["title"] for r in out["results"]] == ["Pump flow"]
        row = out["results"][0]
        assert row["matched"] == "name" and row["url"] == f"#/chart/{row['id']}"

        out = engine.search("pumps", _scope(), kinds=["chart"])  # the group name: contents
        assert [r["matched"] for r in out["results"]] == ["contents"]

        out = engine.search("pump", _scope(principal=""), kinds=["chart"])
        assert out["results"] == [] and "chart" in out["skipped"]


# -- people and settings -------------------------------------------------------------


@pytest.mark.acceptance("MVP-01-A1")
class TestPeopleAndSettingsMVP01A1:
    def test_mvp_01_a1_people_come_from_the_team_directory_bounded_by_site(self, monkeypatch):
        rows = [
            {
                "kind": "person",
                "handle": "@ana:s1",
                "name": "Ana Reyes",
                "roles": ["site-admin"],
                "site": "s1",
            },
            {
                "kind": "person",
                "handle": "@ann:s2",
                "name": "Ann Other",
                "roles": ["site-admin"],
                "site": "s2",
            },
        ]
        monkeypatch.setattr(engine, "_people", lambda: rows)
        out = engine.search("an", _scope(), kinds=["person"])
        assert [r["id"] for r in out["results"]] == ["@ana:s1"]
        assert out["results"][0]["url"] == "#/team/@ana:s1"
        out = engine.search("site-admin", _scope(), kinds=["person"])
        assert [r["matched"] for r in out["results"]] == ["contents"]

    def test_mvp_01_a1_without_a_team_directory_people_are_skipped_with_the_reason(
        self, monkeypatch
    ):
        import sys

        monkeypatch.setitem(sys.modules, "axiom.extensions.builtins.actors.skills", None)
        out = engine.search("ana", _scope(), kinds=["person"])
        assert out["results"] == [] and "people directory" in out["skipped"]["person"]

    def test_mvp_01_a1_settings_match_by_key_never_by_value(self, monkeypatch):
        from axiom.extensions.builtins.settings import store

        monkeypatch.setattr(
            store.SettingsStore, "all", lambda self: {"chat.model": "x", "db.url": "postgres://rod"}
        )
        assert engine.search("rod", _scope(), kinds=["setting"])["results"] == []
        out = engine.search("model", _scope(), kinds=["setting"])
        assert [(r["id"], r["matched"], r["snippet"]) for r in out["results"]] == [
            ("chat.model", "name", "")
        ]


# -- the scope selector's site (MVP-01-A3) -------------------------------------------


@pytest.fixture
def http(monkeypatch):
    seen: list[frozenset | None] = []

    def provider(q, scope, limit):
        seen.append(scope.sites)
        return [
            engine.Hit(kind="conversation", id=f"c-{s}", title="rod", site=s, matched="name")
            for s in sorted(scope.sites or {"s1", "s2"})
        ]

    monkeypatch.setattr(engine, "PROVIDERS", {"conversation": provider})
    monkeypatch.setenv("AXIOM_SERVED_SITES", "s1,s2")
    router = APIRouter()
    api.register_routes(router)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app), seen


@pytest.mark.acceptance("MVP-01-A3")
class TestScopeMVP01A3:
    def test_mvp_01_a3_no_site_searches_every_site_the_request_may_see(self, http):
        client, seen = http
        body = client.get("/api/v1/search", params={"q": "rod", "kinds": "conversation"}).json()
        assert {r["site"] for r in body["results"]} == {"s1", "s2"}
        assert seen[-1] == frozenset({"s1", "s2"})

    def test_mvp_01_a3_the_selected_site_narrows_the_results(self, http):
        client, seen = http
        for path in ("/api/v1/search", "/api/v1/search/suggest"):
            body = client.get(path, params={"q": "rod", "site": "s2"}).json()
            assert [r["id"] for r in body["results"]] == ["c-s2"]
            assert body["counts"]["kind"] == {"conversation": 1}
            assert seen[-1] == frozenset({"s2"})

    def test_mvp_01_a3_a_site_outside_the_askers_set_is_an_empty_answer(self, http):
        client, seen = http
        before = len(seen)
        for path in ("/api/v1/search", "/api/v1/search/suggest"):
            r = client.get(path, params={"q": "rod", "site": "s9"})
            assert r.status_code == 200
            body = r.json()
            assert body["results"] == [] and body["counts"]["kind"] == {}
        assert len(seen) == before  # no provider was asked
