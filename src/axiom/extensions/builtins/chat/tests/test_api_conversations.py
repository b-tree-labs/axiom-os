# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The /api/v1/chat conversation CRUD surface (C2 ②).

Exercises the contributed router against a SQLite-backed store (the engine
seam), the appkit contract shapes (account_id ↔ tenant_id), and the site_scope
bound (out of scope = 404, never 403; unlisted, not just unreadable).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.chat import api
from axiom.extensions.builtins.chat.db_models import Base


def _frames(sse_text):
    """Parse a Server-Sent-Events body into the list of data-frame dicts."""
    return [json.loads(line[6:]) for line in sse_text.splitlines() if line.startswith("data: ")]


def _fake_runner(session, content, render):
    """Stand in for the real agent turn: append the turn + drive the render."""
    session.add_message("user", content)
    render.stream_text(iter([SimpleNamespace(type="text", text="Hi ")]))
    render.render_tool_start("get_weather", {"loc": "TX"})
    render.render_tool_result("get_weather", {"ok": True}, 0.1)
    render.stream_text(iter([SimpleNamespace(type="text", text="there")]))
    session.add_message("assistant", "Hi there")
    return "Hi there"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("AXIOM_SERVED_SITES", raising=False)  # unbounded dev box
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    Base.metadata.create_all(engine)
    api.set_test_engine(engine)
    router = APIRouter(prefix="/api/v1")
    api.register_routes(router, subpath="/chat")
    app = FastAPI()
    app.include_router(router)
    try:
        yield TestClient(app)
    finally:
        api.set_test_engine(None)
        engine.dispose()


def _ids(resp):
    return [c["id"] for c in resp.json()["conversations"]]


def test_crud_cycle(client):
    r = client.post("/api/v1/chat/conversations", json={"account_id": "acct1", "title": "Field notes"})
    assert r.status_code == 201, r.text
    conv = r.json()
    cid = conv["id"]
    assert conv["title"] == "Field notes"
    assert conv["account_id"] == "acct1"
    assert conv["starred"] is False

    assert cid in _ids(client.get("/api/v1/chat/conversations"))

    r = client.get("/api/v1/chat/conversations/" + cid)
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["conversation"]["id"] == cid
    assert detail["messages"] == []
    assert detail["message_count"] == 0

    r = client.patch(
        "/api/v1/chat/conversations/" + cid,
        json={"starred": True, "title": "Renamed", "account_id": "acct2"},
    )
    assert r.status_code == 200, r.text
    upd = r.json()
    assert upd["starred"] is True
    assert upd["title"] == "Renamed"
    assert upd["account_id"] == "acct2"  # moved

    r = client.delete("/api/v1/chat/conversations/" + cid)
    assert r.status_code == 204
    assert cid not in _ids(client.get("/api/v1/chat/conversations"))  # archived


def test_list_scoped_by_account(client):
    a = client.post("/api/v1/chat/conversations", json={"account_id": "a1", "title": "one"}).json()["id"]
    b = client.post("/api/v1/chat/conversations", json={"account_id": "a2", "title": "two"}).json()["id"]
    assert _ids(client.get("/api/v1/chat/conversations", params={"account_id": "a1"})) == [a]
    assert _ids(client.get("/api/v1/chat/conversations", params={"account_id": "a2"})) == [b]
    assert set(_ids(client.get("/api/v1/chat/conversations"))) == {a, b}


def test_search_filters_title(client):
    client.post("/api/v1/chat/conversations", json={"title": "soil report"})
    client.post("/api/v1/chat/conversations", json={"title": "weather"})
    hits = client.get("/api/v1/chat/conversations", params={"search": "soil"}).json()["conversations"]
    assert [c["title"] for c in hits] == ["soil report"]


def test_missing_is_404(client):
    assert client.get("/api/v1/chat/conversations/nope").status_code == 404
    assert client.patch("/api/v1/chat/conversations/nope", json={"title": "x"}).status_code == 404
    assert client.delete("/api/v1/chat/conversations/nope").status_code == 404


def test_principal_isolation_same_site(client, monkeypatch):
    # Two principals on the same (unbounded) node — one must never list, GET,
    # PATCH, or DELETE the other's conversation. Nothing else catches a swapped
    # filter: the site tests pass either way.
    who = {"p": "@a:x"}
    monkeypatch.setattr(api, "_principal", lambda request: who["p"])

    a = client.post("/api/v1/chat/conversations", json={"title": "A"}).json()["id"]
    who["p"] = "@b:y"
    b = client.post("/api/v1/chat/conversations", json={"title": "B"}).json()["id"]

    # B sees only B, and cannot touch A by any verb.
    assert _ids(client.get("/api/v1/chat/conversations")) == [b]
    assert client.get("/api/v1/chat/conversations/" + a).status_code == 404
    assert client.patch("/api/v1/chat/conversations/" + a, json={"title": "x"}).status_code == 404
    assert client.delete("/api/v1/chat/conversations/" + a).status_code == 404

    # A sees only A.
    who["p"] = "@a:x"
    assert _ids(client.get("/api/v1/chat/conversations")) == [a]
    assert client.get("/api/v1/chat/conversations/" + b).status_code == 404


def test_site_scope_bounds_reads(client, monkeypatch):
    # A node bound to siteA creates + serves siteA conversations.
    monkeypatch.setenv("AXIOM_SERVED_SITES", "siteA")
    r = client.post("/api/v1/chat/conversations", json={"title": "A-chat"})
    cid = r.json()["id"]
    assert r.json()["site_id"] == "siteA"
    assert cid in _ids(client.get("/api/v1/chat/conversations"))

    # The same node now bound to a different site must neither list nor open it —
    # 404, never 403 (no cross-site disclosure).
    monkeypatch.setenv("AXIOM_SERVED_SITES", "siteB")
    assert client.get("/api/v1/chat/conversations/" + cid).status_code == 404
    assert cid not in _ids(client.get("/api/v1/chat/conversations"))


def test_send_message_streams_frames_and_persists(client, monkeypatch):
    monkeypatch.setattr(api, "_turn_runner", _fake_runner)
    cid = client.post("/api/v1/chat/conversations", json={"title": "t"}).json()["id"]

    r = client.post("/api/v1/chat/conversations/" + cid + "/messages?stream=true", json={"content": "weather?"})
    assert r.status_code == 200, r.text
    frames = _frames(r.text)
    assert {"conversation_id": cid} in frames  # id first, so the client can adopt it
    assert {"chunk": "Hi "} in frames
    assert {"tool_call": "get_weather"} in frames
    assert {
        "tool_result": "get_weather",
        "tool_outcome": {"name": "get_weather", "ok": True, "elapsed": 0.1},
    } in frames
    assert {"chunk": "there"} in frames

    # The turn is persisted to the shared store (append-only) — this is the
    # cross-device sync write: another surface now GETs the same transcript.
    detail = client.get("/api/v1/chat/conversations/" + cid).json()
    roles = [(m["role"], m["content"]) for m in detail["messages"]]
    assert ("user", "weather?") in roles
    assert ("assistant", "Hi there") in roles


def test_send_error_becomes_an_error_frame_not_a_500(client, monkeypatch):
    def boom(session, content, render):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(api, "_turn_runner", boom)
    cid = client.post("/api/v1/chat/conversations", json={}).json()["id"]
    r = client.post("/api/v1/chat/conversations/" + cid + "/messages?stream=true", json={"content": "x"})
    assert r.status_code == 200  # the stream opened; the failure rides in-band
    assert any(f.get("error") == "gateway down" for f in _frames(r.text))


def test_send_to_missing_conversation_is_404(client):
    r = client.post("/api/v1/chat/conversations/nope/messages", json={"content": "x"})
    assert r.status_code == 404


def test_guest_message_streams_without_persisting(client, monkeypatch):
    monkeypatch.setattr(api, "_turn_runner", _fake_runner)
    r = client.post("/api/v1/chat/guest/message", json={"message": "hi"})
    assert r.status_code == 200, r.text
    frames = _frames(r.text)
    assert any("conversation_id" in f for f in frames)
    assert {"chunk": "Hi "} in frames
    # Guest history is client-side; nothing lands in the shared store.
    assert client.get("/api/v1/chat/conversations").json()["conversations"] == []


def test_feedback_records_and_is_scoped(client, monkeypatch):
    from sqlalchemy.orm import Session as SASession

    from axiom.extensions.builtins.chat.db_models import ChatMessageFeedback

    monkeypatch.setattr(api, "_turn_runner", _fake_runner)
    cid = client.post("/api/v1/chat/conversations", json={"title": "t"}).json()["id"]
    # A turn creates user seq0 + assistant seq1; rate the assistant message.
    client.post("/api/v1/chat/conversations/" + cid + "/messages?stream=true", json={"content": "q"})

    r = client.post(
        "/api/v1/chat/messages/feedback",
        json={"message_id": cid + ":1", "rating": "up", "comment": "great"},
    )
    assert r.status_code == 204

    with SASession(api._TEST_ENGINE) as s:
        fb = s.get(ChatMessageFeedback, (cid, 1, ""))  # tests are unauthenticated → principal ""
        assert fb is not None
        assert fb.rating == "up"
        assert fb.comment == "great"

    # Re-rating upserts (no duplicate, updates in place).
    r = client.post("/api/v1/chat/messages/feedback", json={"message_id": cid + ":1", "rating": "down"})
    assert r.status_code == 204
    with SASession(api._TEST_ENGINE) as s:
        assert s.get(ChatMessageFeedback, (cid, 1, "")).rating == "down"


def test_feedback_on_unknown_or_malformed_is_404(client):
    assert client.post("/api/v1/chat/messages/feedback", json={"message_id": "nope:1", "rating": "up"}).status_code == 404
    assert client.post("/api/v1/chat/messages/feedback", json={"message_id": "no-colon", "rating": "up"}).status_code == 404


def test_feedback_respects_site_scope(client, monkeypatch):
    monkeypatch.setenv("AXIOM_SERVED_SITES", "siteA")
    cid = client.post("/api/v1/chat/conversations", json={"title": "A"}).json()["id"]
    # Rebind the node to another site — the conversation is now out of scope.
    monkeypatch.setenv("AXIOM_SERVED_SITES", "siteB")
    r = client.post("/api/v1/chat/messages/feedback", json={"message_id": cid + ":0", "rating": "up"})
    assert r.status_code == 404
