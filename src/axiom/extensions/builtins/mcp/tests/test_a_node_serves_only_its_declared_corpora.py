# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A node that declares its served corpora keeps MCP retrieval inside them.

The MCP retrieve tool searched every corpus when a caller named none, so a
corpus a node never meant to serve (a review queue, material held back for its
owners) was reachable by any caller with read access. ``AXIOM_RAG_SERVED_CORPORA``
names the corpora a node serves: an unnamed request searches exactly those, and a
request naming any other corpus is refused, not narrowed. A node that does not
set it keeps the old behaviour (a personal install searching its own corpora).
"""
from __future__ import annotations

import asyncio

from axiom.extensions.builtins.mcp import platform_primitives as pp
from axiom.rag.store import SearchResult


class _RecordingStore:
    does_own_hybrid = False

    def __init__(self):
        self.searched: list = []

    def search(self, query_embedding=None, query_text="", corpora=None, limit=5, **kw):
        self.searched.append(None if corpora is None else sorted(corpora))
        return [SearchResult("doc.md", "Doc", "text", 0, 0.9, 0.9, "rag-org")]

    def access_tier_for(self, path):
        return "public"


def _run(monkeypatch, args, served):
    store = _RecordingStore()
    monkeypatch.setattr(pp, "_resolve_rag_store", lambda: store)
    monkeypatch.setattr(pp, "_embed_query", lambda _t: None)
    if served is None:
        monkeypatch.delenv("AXIOM_RAG_SERVED_CORPORA", raising=False)
    else:
        monkeypatch.setenv("AXIOM_RAG_SERVED_CORPORA", served)
    return store, asyncio.run(pp._rag_retrieve(args))


def test_an_unnamed_request_searches_only_the_served_corpora(monkeypatch):
    store, r = _run(monkeypatch, {"query": "x"}, "rag-community, rag-org")
    assert r["ok"]
    assert store.searched and all(c == ["rag-community", "rag-org"] for c in store.searched)


def test_a_request_for_a_corpus_the_node_does_not_serve_is_refused(monkeypatch):
    store, r = _run(monkeypatch, {"query": "x", "corpora": ["rag-org", "restricted"]},
                    "rag-community,rag-org")
    assert r["ok"] is False
    assert "restricted" in r["error"] and not r["results"]
    assert store.searched == [], "a refused request must not reach the store"


def test_a_request_inside_the_served_corpora_is_kept_as_named(monkeypatch):
    store, r = _run(monkeypatch, {"query": "x", "corpora": ["rag-org"]}, "rag-community,rag-org")
    assert r["ok"] and all(c == ["rag-org"] for c in store.searched)


def test_a_node_that_declares_nothing_keeps_todays_behaviour(monkeypatch):
    store, r = _run(monkeypatch, {"query": "x"}, None)
    assert r["ok"] and all(c is None for c in store.searched)
