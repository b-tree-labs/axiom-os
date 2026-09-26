# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Perf guards for the /api/v1/chat surface (C2 — DoD perf pass).

These are REGRESSION guards, not SLAs: they run against SQLite + a stubbed turn
runner (no live LLM), so the absolute numbers are not production latency — but
the SHAPE is. They catch an O(n^2) creeping into listing, a per-request cost
blowup in create, or the SSE path buffering instead of streaming. Actuals print
with `-s`. Real end-to-end latency (LLM + RAG + gold) is a live-stack measurement,
not this.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from axiom.extensions.builtins.chat import api
from axiom.extensions.builtins.chat.db_models import Base


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("AXIOM_SERVED_SITES", raising=False)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True
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


def _stub_runner(session, content, render):
    """A canned streamed turn: 40 text chunks + a couple of tool frames."""
    session.add_message("user", content)
    render.render_tool_start("get_weather", {})
    for i in range(40):
        render.stream_text(iter([SimpleNamespace(type="text", text=f"tok{i} ")]))
    render.render_tool_result("get_weather", {"ok": True}, 0.01)
    session.add_message("assistant", "done")
    return "done"


def test_list_scales_sublinearly(client):
    """Listing N conversations stays well under a regression ceiling (guards the
    scoped-read index; a full-scan O(n) or a per-row query would blow this)."""
    n = 300
    for i in range(n):
        client.post("/api/v1/chat/conversations", json={"title": f"c{i}", "account_id": "nos"})
    t = time.perf_counter()
    r = client.get("/api/v1/chat/conversations", params={"account_id": "nos"})
    dt = time.perf_counter() - t
    assert r.status_code == 200 and len(r.json()["conversations"]) == n
    print(f"\n[perf] list {n} conversations: {dt * 1000:.1f} ms")
    assert dt < 2.0, f"list of {n} took {dt:.2f}s — regression"


def test_create_latency(client):
    times = []
    for i in range(50):
        t = time.perf_counter()
        r = client.post("/api/v1/chat/conversations", json={"title": f"c{i}", "account_id": "nos"})
        times.append(time.perf_counter() - t)
        assert r.status_code == 201
    times.sort()
    p50, p95 = times[len(times) // 2], times[int(len(times) * 0.95)]
    print(f"\n[perf] create p50={p50*1000:.1f}ms p95={p95*1000:.1f}ms (n=50)")
    assert p95 < 0.5, f"create p95 {p95:.3f}s — regression"


def test_stream_first_frame_and_throughput(client, monkeypatch):
    """SSE must stream, not buffer: conversation_id lands first, then chunks."""
    monkeypatch.setattr(api, "_turn_runner", _stub_runner)
    cid = client.post("/api/v1/chat/conversations", json={"account_id": "nos"}).json()["id"]
    t = time.perf_counter()
    r = client.post(f"/api/v1/chat/conversations/{cid}/messages?stream=true", json={"content": "hi"})
    total = time.perf_counter() - t
    assert r.status_code == 200
    frames = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
    assert frames[0] == {"conversation_id": cid}  # id first — client can adopt immediately
    chunks = [f for f in frames if "chunk" in f]
    assert len(chunks) == 40
    print(f"\n[perf] stream turn: {len(frames)} frames in {total*1000:.1f} ms ({len(frames)/total:.0f} frames/s)")
    assert total < 2.0, f"stubbed stream took {total:.2f}s — SSE may be buffering"


def test_send_throughput_sequential(client, monkeypatch):
    """Sustained send throughput (append-only persistence shouldn't degrade)."""
    monkeypatch.setattr(api, "_turn_runner", _stub_runner)
    cid = client.post("/api/v1/chat/conversations", json={"account_id": "nos"}).json()["id"]
    n = 30
    t = time.perf_counter()
    for _ in range(n):
        r = client.post(f"/api/v1/chat/conversations/{cid}/messages?stream=true", json={"content": "q"})
        assert r.status_code == 200
    elapsed = time.perf_counter() - t
    print(f"\n[perf] {n} sends on one conversation: {elapsed*1000:.0f} ms ({n/elapsed:.1f} turns/s)")
    # append-only writes → roughly linear; guard against an O(n^2) rewrite creeping in.
    assert elapsed < n * 0.25, f"{n} sends took {elapsed:.2f}s — per-turn cost is growing"
    detail = client.get(f"/api/v1/chat/conversations/{cid}").json()
    assert detail["message_count"] == n * 2  # each turn = user + assistant
