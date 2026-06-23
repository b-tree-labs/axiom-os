# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the ``axiom_rag__retrieve`` platform primitive (WS-A).

The handler must be domain-agnostic, config-resolved, and fail-soft: an
unconfigured or unavailable RAG store returns a structured note, never a
crash, so an MCP client always gets a well-formed result.
"""

from __future__ import annotations

import asyncio

from axiom.extensions.builtins.mcp import platform_primitives as pp
from axiom.rag.store import SearchResult


def _hit(path: str = "docs/a.md", text: str = "alpha", idx: int = 0) -> SearchResult:
    return SearchResult(
        source_path=path,
        source_title="A Title",
        chunk_text=text,
        chunk_index=idx,
        similarity=0.9,
        combined_score=0.9,
        corpus="rag-internal",
    )


class _FakeStore:
    def __init__(self, hits: list[SearchResult], raises: bool = False) -> None:
        self._hits = hits
        self._raises = raises

    def search(self, query_embedding=None, query_text="", corpora=None, limit=5, **kw):
        if self._raises:
            raise RuntimeError("store boom")
        return list(self._hits)


def test_no_store_configured_is_fail_soft(monkeypatch):
    monkeypatch.setattr(pp, "_resolve_rag_store", lambda: None)
    out = asyncio.run(pp._rag_retrieve({"query": "hello"}))
    assert out["ok"] is False
    assert out["results"] == []
    blob = (out.get("note", "") + out.get("error", "")).lower()
    assert "configur" in blob  # tells the caller how to fix it


def test_empty_query_rejected(monkeypatch):
    # Must reject before resolving a store.
    def _boom():
        raise AssertionError("store should not be resolved for an empty query")

    monkeypatch.setattr(pp, "_resolve_rag_store", _boom)
    out = asyncio.run(pp._rag_retrieve({"query": "   "}))
    assert out["ok"] is False
    assert "query" in out.get("error", "").lower()


def test_returns_serialized_chunks(monkeypatch):
    store = _FakeStore([_hit(text="alpha"), _hit(path="docs/b.md", text="beta", idx=1)])
    monkeypatch.setattr(pp, "_resolve_rag_store", lambda: store)
    monkeypatch.setattr(pp, "_embed_query", lambda q: None)  # force text-only path
    out = asyncio.run(pp._rag_retrieve({"query": "alpha beta", "k": 5}))
    assert out["ok"] is True
    assert out["count"] == 2
    first = out["results"][0]
    assert first["citation_key"] == "C1"
    assert first["text"]
    assert "score" in first
    assert "source_path" in first


def test_hybrid_capable_store_skips_local_embedding(monkeypatch):
    # A store that does hybrid server-side (e.g. a remote peer) must not be
    # locally embedded — that would force a wasteful second round-trip.
    store = _FakeStore([_hit(text="alpha")])
    store.does_own_hybrid = True
    monkeypatch.setattr(pp, "_resolve_rag_store", lambda: store)

    def _embed_must_not_run(_q):
        raise AssertionError("local embedding must be skipped for a hybrid store")

    monkeypatch.setattr(pp, "_embed_query", _embed_must_not_run)
    out = asyncio.run(pp._rag_retrieve({"query": "alpha", "k": 3}))
    assert out["ok"] is True
    assert out["mode"] == "text"  # single text round-trip; peer does the hybrid


def test_retrieve_error_is_fail_soft(monkeypatch):
    monkeypatch.setattr(pp, "_resolve_rag_store", lambda: _FakeStore([], raises=True))
    monkeypatch.setattr(pp, "_embed_query", lambda q: None)
    out = asyncio.run(pp._rag_retrieve({"query": "x"}))
    assert out["ok"] is False
    assert out["results"] == []
    assert "boom" in out.get("error", "").lower() or "runtimeerror" in out.get("error", "").lower()
