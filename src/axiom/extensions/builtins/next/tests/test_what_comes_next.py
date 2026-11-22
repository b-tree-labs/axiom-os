# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One ranked list of what comes next, with the reference's rules and two safety properties.

The rules (SoilMetrix action cards): card 1 is the most forward-looking
prompt; at most five; only what exists here; nothing on failure. The safety
properties: nobody's history is read without knowing whose it is, and a
destructive capability is never a one-click card.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from axiom.extensions.builtins.next import engine
from axiom.extensions.builtins.next.skills import _principal, bind, run
from axiom.infra.skill_declarations import NextStep
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec


def _card(i, kind="action", priority=5, label=None):
    return engine.Card(
        id=f"c{i}", label=label or f"Card {i}", kind=kind, why="w", priority=priority
    )


class TestTheRanking:
    def test_card_one_is_the_best_prompt_even_when_an_action_outranks_it(self):
        ranked = engine.rank([_card(1, "action", 9), _card(2, "prompt", 5), _card(3, "action", 7)])
        assert [c.id for c in ranked] == ["c2", "c1", "c3"]

    def test_at_most_five_and_no_duplicate_labels(self):
        cards = [_card(i, priority=i) for i in range(10)] + [_card(99, label="Card 9")]
        ranked = engine.rank(cards)
        assert len(ranked) == 5
        assert [c.label for c in ranked].count("Card 9") == 1

    def test_a_failing_source_yields_nothing_from_it_and_says_so(self):
        def boom(asker):
            raise RuntimeError("store down")

        out = engine.suggest(
            engine.Asker(),
            providers={"boom": boom, "default": engine.default_first},
            extensions=False,
        )
        assert [c["id"] for c in out["cards"]] == ["attention"]
        assert "store down" in out["skipped"]["boom"]


class TestAppState:
    def test_what_is_open_leads_and_only_its_label_is_shown(self):
        cards = engine.from_view(
            engine.Asker(view={"surface": "chart", "label": "Pool temperature", "summary": "S"})
        )
        assert cards[0].label == "What should I look at next in Pool temperature?"
        assert all(
            "S" not in c.label.split() for c in cards
        )  # the summary never becomes visible text

    def test_nothing_open_offers_nothing_from_the_view(self):
        assert engine.from_view(engine.Asker(view=None)) == []


class TestHistory:
    def test_a_conversation_left_a_while_ago_is_offered_to_pick_up(self, monkeypatch):
        now = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)

        class Store:
            def list_meta(self, *, principal, limit):
                assert principal == "@ada:local"
                return [
                    {
                        "id": "fresh",
                        "title": "Just now",
                        "updated_at": (now - timedelta(minutes=2)).isoformat(),
                    },
                    {
                        "id": "c7",
                        "title": "Pump trend",
                        "updated_at": (now - timedelta(hours=3)).isoformat(),
                    },
                ]

        monkeypatch.setattr(
            "axiom.infra.orchestrator.conversation_store.resolve_conversation_store",
            lambda engine_=None: Store(),
        )
        (card,) = engine.from_conversations(engine.Asker(principal="@ada:local"), now=now)
        assert card.label == "Pick up “Pump trend”" and card.payload == {"conversation_id": "c7"}

    def test_no_principal_reads_no_history(self):
        assert engine.from_conversations(engine.Asker(principal="")) == []
        assert engine.from_recent_actions(engine.Asker(principal="")) == []

    def test_declared_follow_ups_are_offered_only_if_they_exist_and_are_not_destructive(self):
        reg = SkillRegistry()

        def noop(params, ctx):
            return SkillResult(value={})

        reg.register_skill(
            SkillSpec(
                name="a.run",
                fn=noop,
                description="Run a.",
                next_steps=(
                    NextStep(target="b.review", why="Review what a.run produced"),
                    NextStep(target="c.purge", why="Purge"),
                    NextStep(target="missing.skill", why="Not here"),
                ),
            )
        )
        reg.register_skill(
            SkillSpec(name="b.review", fn=noop, description="Review the results. More text.")
        )
        reg.register_skill(
            SkillSpec(name="c.purge", fn=noop, description="Purge everything.", risk="destructive")
        )

        def recent(n, agent):
            assert agent == "@ada:local"
            return {"actions": [{"name": "a.run", "outcome": "ok"}]}

        cards = engine.from_recent_actions(
            engine.Asker(principal="@ada:local"), registry=reg, recent=recent
        )
        assert [c.payload for c in cards] == [{"skill": "b.review"}]
        assert (
            cards[0].label == "Review the results" and cards[0].why == "Review what a.run produced"
        )


class TestTheSurfaces:
    def test_a_stated_empty_principal_is_nobody_never_the_machine_operator(self):
        assert _principal({"_principal": ""}) == ""
        assert _principal({"_principal": "@ada:local"}) == "@ada:local"

    def test_the_skill_accepts_view_context_as_json_text_from_mcp(self, monkeypatch):
        monkeypatch.setattr(engine, "_extension_providers", lambda: [])
        reg = SkillRegistry()
        bind(reg)
        ctx = SkillContext(registry=reg, state_dir=None, logger=logging.getLogger("t"))
        monkeypatch.setattr(
            engine, "PROVIDERS", {"view": engine.from_view, "default": engine.default_first}
        )
        r = run({"_principal": "", "view_context": '{"label": "Pump trend"}'}, ctx)
        assert r.value["cards"][0]["label"] == "What should I look at next in Pump trend?"

    def test_the_web_route_reads_the_session_not_the_operator(self, monkeypatch):
        from fastapi import APIRouter, FastAPI
        from fastapi.testclient import TestClient

        from axiom.extensions.builtins.next import api

        seen = {}

        def fake_run(params, ctx):
            seen.update(params)
            return SkillResult(value={"cards": [], "skipped": {}})

        monkeypatch.setattr("axiom.extensions.builtins.next.skills.run", fake_run)
        monkeypatch.setattr(api, "_claims", lambda request: {})
        router = APIRouter()
        api.register_routes(router)
        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        assert (
            TestClient(app).post("/api/v1/next", json={"view_context": {"label": "x"}}).status_code
            == 200
        )
        assert seen["_principal"] == "" and seen["view_context"] == {"label": "x"}
