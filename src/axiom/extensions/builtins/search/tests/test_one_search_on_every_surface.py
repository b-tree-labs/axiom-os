# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One search, the same rows on every surface, bounded by what the asker may read.

The app's search "did not work": the palette's only source matched chat
TITLES, so anything said inside a conversation, any logbook entry and any
document were unreachable. These tests pin the engine's contract (merge, rank,
cap, never let one source sink the rest, say what was not searched), the
skill's input handling across transports, and that the HTTP route and the CLI
are thin faces over the same skill.
"""

from __future__ import annotations

import logging

import pytest

from axiom.extensions.builtins.search import cli, engine
from axiom.extensions.builtins.search.skills import _as_list, bind, run
from axiom.infra.skills import SkillContext, SkillRegistry


def _fake(kind, *titles, score=0.5):
    def provider(q, scope, limit):
        return [
            engine.Hit(kind=kind, id=f"{kind}-{i}", title=t, score=score)
            for i, t in enumerate(titles)
        ][:limit]

    return provider


def _boom(q, scope, limit):
    raise RuntimeError("store down")


def _skip(q, scope, limit):
    raise engine._Skip("no RAG store configured")


SCOPE = engine.Scope(principal="@ben:local", sites=frozenset({"s1"}))


class TestTheEngine:
    def test_it_merges_providers_and_ranks_by_score(self):
        out = engine.search(
            "rod",
            SCOPE,
            providers={
                "conversation": _fake("conversation", "a", score=0.4),
                "logbook": _fake("logbook", "b", score=0.9),
            },
            kinds=["conversation", "logbook"],
        )
        assert [r["title"] for r in out["results"]] == ["b", "a"]

    def test_one_failing_source_does_not_sink_the_rest_and_is_named(self):
        out = engine.search(
            "rod",
            SCOPE,
            providers={"conversation": _fake("conversation", "a"), "logbook": _boom},
            kinds=["conversation", "logbook"],
        )
        assert [r["title"] for r in out["results"]] == ["a"]
        assert "logbook" in out["skipped"] and "store down" in out["skipped"]["logbook"]

    def test_an_unconfigured_source_says_why_rather_than_reading_as_no_matches(self):
        out = engine.search("rod", SCOPE, providers={"corpus": _skip}, kinds=["corpus"])
        assert out["results"] == []
        assert out["skipped"] == {"corpus": "no RAG store configured"}

    def test_suggest_leaves_out_the_slow_corpus_and_caps_at_eight(self):
        calls = []

        def corpus(q, s, n):
            calls.append("corpus")
            return []

        out = engine.search(
            "rod",
            SCOPE,
            mode=engine.SUGGEST,
            providers={
                "conversation": _fake("conversation", *[f"c{i}" for i in range(20)]),
                "logbook": _fake("logbook"),
                "corpus": corpus,
            },
        )
        assert calls == []
        assert len(out["results"]) == 8

    def test_a_one_letter_query_returns_nothing_without_touching_a_store(self):
        out = engine.search("r", SCOPE, providers={"conversation": _boom}, kinds=["conversation"])
        assert out == {"query": "r", "mode": "full", "results": [], "skipped": {}}

    def test_title_hits_outrank_body_hits(self):
        assert engine._score("rod", "control rod", "") > engine._score(
            "rod", "morning check", "the rod"
        )


def _ctx():
    reg = SkillRegistry()
    bind(reg)
    return SkillContext(registry=reg, state_dir=None, logger=logging.getLogger("t"))


class TestTheSkill:
    @pytest.mark.parametrize(
        ("raw", "want"),
        [
            (None, []),
            ("logbook", ["logbook"]),
            ("logbook,corpus", ["logbook", "corpus"]),
            ('["corpus"]', ["corpus"]),
            (["corpus"], ["corpus"]),
        ],
    )
    def test_kinds_arrive_as_a_list_from_any_transport(self, raw, want):
        assert _as_list(raw) == want

    def test_it_refuses_an_empty_query_by_saying_what_to_do(self):
        r = run({"query": "  "}, _ctx())
        assert not r.ok and "query is required" in r.errors[0]

    def test_it_refuses_an_unknown_kind_and_lists_the_real_ones(self):
        r = run({"query": "rod", "kinds": "planets"}, _ctx())
        assert not r.ok and "conversation" in r.errors[0]

    def test_it_is_registered_read_only_for_cli_and_mcp(self):
        reg = SkillRegistry()
        assert bind(reg) == ["search.query"]
        assert reg.has("search.query")


class TestTheSurfaces:
    def test_the_cli_strips_the_verb_the_user_typed(self, monkeypatch, capsys):
        seen = {}

        def fake_run(params, ctx):
            seen.update(params)
            from axiom.infra.skills import SkillResult

            return SkillResult(value={"query": params["query"], "results": [], "skipped": {}})

        monkeypatch.setattr("axiom.extensions.builtins.search.skills.run", fake_run)
        monkeypatch.setattr("sys.argv", ["axi", "search", "control", "rod"])
        monkeypatch.setattr(
            "axiom.extensions.builtins.search.cli.invoke_capability",
            lambda reg, name, params, ctx, surface: fake_run(params, ctx),
        )
        assert cli.main() == 0
        assert seen["query"] == "control rod"

    def test_the_cli_says_what_was_not_searched_and_what_to_do_next(self):
        text = cli.render(
            {"query": "rod", "results": [], "skipped": {"corpus": "no RAG store configured"}}
        )
        assert 'Nothing on this node matches "rod"' in text
        assert "not searched: corpus" in text
        assert "Next:" in text

    def test_the_http_route_is_a_thin_face_over_the_skill(self, monkeypatch):
        from fastapi import APIRouter, FastAPI
        from fastapi.testclient import TestClient

        from axiom.extensions.builtins.search import api

        monkeypatch.setattr(
            engine,
            "PROVIDERS",
            {
                "conversation": _fake("conversation", "control rod notes"),
                "logbook": _fake("logbook"),
                "corpus": _fake("corpus"),
            },
        )
        router = APIRouter()
        api.register_routes(router)
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        client = TestClient(app)

        full = client.get("/api/v1/search", params={"q": "control rod"}).json()
        assert full["results"][0]["title"] == "control rod notes"
        sug = client.get("/api/v1/search/suggest", params={"q": "c"}).json()
        assert sug["results"] == []
        assert (
            client.get("/api/v1/search", params={"q": "rod", "kinds": "planets"}).status_code == 400
        )
