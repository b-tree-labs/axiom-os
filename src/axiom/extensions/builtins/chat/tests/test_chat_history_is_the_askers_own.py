# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Chat history: a person's own sessions, per variant, one list on every surface (MVP-05-A1),
and a session that remembers the screen it was asked from (MVP-05-A2).
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.chat import api
from axiom.extensions.builtins.chat import skills as chat_skills
from axiom.extensions.builtins.chat.db_models import Base
from axiom.extensions.builtins.chat.skills import history as hist
from axiom.infra.skills import SkillContext, SkillRegistry

ALICE = "@alice:local"
BOB = "@bob:local"


@pytest.fixture
def chat(monkeypatch, tmp_path):
    monkeypatch.delenv("AXIOM_SERVED_SITES", raising=False)
    monkeypatch.delenv("AXIOM_VARIANT", raising=False)
    monkeypatch.setenv("AXIOM_VARIANTS_CONFIG", str(tmp_path / "none.toml"))  # the built-ins
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
    )
    Base.metadata.create_all(engine)
    api.set_test_engine(engine)
    store = api._store()

    def add(principal, title, text="hello", tenant=""):
        s = store.create(principal_id=principal, tenant_id=tenant)
        s.add_message("user", text)
        store.save(s)
        store.rename(s.session_id, title)
        return s.session_id

    try:
        yield add
    finally:
        api.set_test_engine(None)
        api.reset_turn_runner()
        engine.dispose()


def _ids(params):
    result = hist.history(params)
    assert result.ok, result.errors
    return [r["id"] for r in result.value["sessions"]]


@pytest.mark.acceptance("MVP-05-A1")
class TestOwnSessionsMVP05A1:
    def test_mvp_05_a1_another_persons_sessions_never_appear(self, chat):
        first = chat(ALICE, "first")
        chat(BOB, "bob's")
        second = chat(ALICE, "second")
        assert _ids({"_principal": ALICE}) == [second, first]  # newest first
        assert chat(BOB, "again") not in _ids({"_principal": ALICE})

    def test_mvp_05_a1_nobody_asking_is_refused_not_shown_everything(self, chat):
        chat(ALICE, "first")
        result = hist.history({"_principal": ""})
        assert not result.ok and "sign in" in result.errors[0]

    def test_mvp_05_a1_q_matches_a_title_by_name_or_a_message_in_contents(self, chat):
        by_name = chat(ALICE, "Pump curve", "nothing")
        in_text = chat(ALICE, "Morning", "the pump tripped at nine")
        chat(BOB, "Pump, Bob's", "pump")
        rows = hist.history({"_principal": ALICE, "q": "pump"}).value["sessions"]
        assert {r["id"]: r["matched"] for r in rows} == {by_name: "name", in_text: "contents"}
        assert "pump tripped" in next(r for r in rows if r["id"] == in_text)["snippet"]

    def test_mvp_05_a1_another_variants_sessions_never_appear(self, chat, monkeypatch):
        plain = chat(ALICE, "plain install")
        mine = chat(ALICE, "in the axiom variant", tenant="variant-axiom")
        other = chat(ALICE, "in the product variant", tenant="variant-product")
        assert _ids({"_principal": ALICE}) == [plain]
        monkeypatch.setenv("AXIOM_VARIANT", "axiom")
        assert _ids({"_principal": ALICE}) == [mine]
        monkeypatch.setenv("AXIOM_VARIANT", "product")
        assert _ids({"_principal": ALICE}) == [other]

    def test_mvp_05_a1_a_conversation_opened_in_a_variant_belongs_to_its_tenant(
        self, chat, monkeypatch
    ):
        monkeypatch.setenv("AXIOM_VARIANT", "axiom")
        monkeypatch.setattr(api, "_principal", lambda request: ALICE)
        client = _client()
        cid = client.post("/api/v1/chat/conversations", json={"account_id": "x"}).json()["id"]
        assert (
            client.get(f"/api/v1/chat/conversations/{cid}").json()["conversation"]["account_id"]
            == "variant-axiom"
        )


def _client():
    router = APIRouter(prefix="/api/v1")
    api.register_routes(router, subpath="/chat")
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.mark.acceptance("MVP-05-A1")
class TestEverySurfaceMVP05A1:
    def test_mvp_05_a1_the_page_the_cli_and_mcp_return_the_same_list(
        self, chat, monkeypatch, capsys
    ):
        chat(ALICE, "pump one", "x")
        chat(BOB, "pump bob", "x")
        chat(ALICE, "pump two", "x")
        want = _ids({"_principal": ALICE, "q": "pump"})
        assert len(want) == 2

        monkeypatch.setattr(api, "_principal", lambda request: ALICE)
        page = _client().get("/api/v1/chat/history", params={"q": "pump"})
        assert page.status_code == 200, page.text
        assert [r["id"] for r in page.json()["sessions"]] == want

        monkeypatch.setattr("axiom.infra.principal.local_handle", lambda: ALICE)
        assert hist.cli_main(["pump", "--json"]) == 0
        assert [r["id"] for r in json.loads(capsys.readouterr().out)["sessions"]] == want

        registry = SkillRegistry()
        chat_skills.bind(registry)
        from axiom.extensions.builtins.mcp.skill_tools import skill_tool_contribution

        ctx = lambda: SkillContext(  # noqa: E731
            registry=registry, state_dir=None, logger=logging.getLogger("t")
        )
        mcp = skill_tool_contribution(registry, ctx_factory=ctx)
        tool = next(t for t in mcp.tools if t.name == "axiom_chat__history")
        assert tool.annotations.read_only_hint
        via = registry.invoke("chat.history", {"q": "pump"}, ctx())
        assert [r["id"] for r in via.value["sessions"]] == want

    def test_mvp_05_a1_the_page_refuses_a_signed_out_browser(self, chat, monkeypatch):
        chat(ALICE, "first")
        monkeypatch.setattr(api, "_principal", lambda request: "")
        assert _client().get("/api/v1/chat/history").status_code == 401

    def test_mvp_05_a1_axi_chat_history_routes_to_the_skill(self, chat, monkeypatch):
        from axiom.extensions.builtins.chat.commands import cli

        seen = {}
        monkeypatch.setattr(hist, "cli_main", lambda argv: seen.setdefault("argv", argv) and 0)
        monkeypatch.setattr("sys.argv", ["axi", "chat", "history", "pump"])
        with pytest.raises(SystemExit):
            cli.main()
        assert seen["argv"] == ["pump"]


# -- MVP-05-A2: resume restores the view context ------------------------------------------------


def _runner(session, content, render, *, tenant_id=None):
    session.add_message("user", content)
    render.stream_text(iter([SimpleNamespace(type="text", text="ok")]))
    session.add_message("assistant", "ok")
    return "ok"


VIEW = {
    "schema": "axiom.view_context/1",
    "app": "web",
    "route": "#/charts",
    "screen": "Charts",
    "focus": "g1",
    "groups": [{"id": "grp", "graphs": []}],
    "actions": ["focus"],
    "summary": "A DESCRIPTION OF THE SCREEN",
}


@pytest.mark.acceptance("MVP-05-A2")
class TestResumeMVP05A2:
    def test_mvp_05_a2_getting_a_session_returns_the_view_it_was_last_asked_from(
        self, chat, monkeypatch
    ):
        api.set_turn_runner(_runner)
        monkeypatch.setattr(api, "_principal", lambda request: ALICE)
        client = _client()
        cid = client.post("/api/v1/chat/conversations", json={}).json()["id"]
        assert client.get(f"/api/v1/chat/conversations/{cid}").json()["view_context"] is None

        client.post(
            f"/api/v1/chat/conversations/{cid}/messages",
            json={"content": "plot it", "view_context": VIEW},
        )
        detail = client.get(f"/api/v1/chat/conversations/{cid}").json()
        assert detail["view_context"]["route"] == "#/charts"
        assert detail["view_context"]["screen"] == "Charts"
        assert detail["view_context"]["focus"] == "g1"
        assert [g["id"] for g in detail["view_context"]["groups"]] == ["grp"]
        # Where the person was, not the prompt's description of it nor the page's offers.
        assert "summary" not in detail["view_context"] and "actions" not in detail["view_context"]
        # Never in the transcript: the messages are what the person said.
        assert all("#/charts" not in m["content"] for m in detail["messages"])

        # A turn that sent no view leaves the last one standing.
        client.post(f"/api/v1/chat/conversations/{cid}/messages", json={"content": "and now"})
        assert (
            client.get(f"/api/v1/chat/conversations/{cid}").json()["view_context"]["route"]
            == "#/charts"
        )
