# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""P8 — the real `axiom_rag__retrieve` primitive filters by access tier.

A broad MCP surface must not leak restricted/EC chunks: it defaults to the
public tier, and a local store that cannot report tiers fails CLOSED.
"""
from __future__ import annotations

import asyncio


from axiom.extensions.builtins.mcp import platform_primitives as pp
from axiom.rag.store import SearchResult


class _FakeStore:
    does_own_hybrid = False

    def search(self, query_embedding=None, query_text="", corpora=None, limit=5, **kw):
        return [
            SearchResult("pub.md", "Public", "public text", 0, 0.9, 0.9, "community"),
            SearchResult("sec.md", "Secret", "restricted text", 0, 0.8, 0.8, "rag-org"),
        ]

    def access_tier_for(self, path):
        return {"pub.md": "public", "sec.md": "restricted"}.get(path)


class _NoTierStore(_FakeStore):
    access_tier_for = None  # local store that can't report tiers


def _run(args):
    return asyncio.run(pp._rag_retrieve(args))


def test_restricted_chunk_is_filtered_out(monkeypatch):
    monkeypatch.setattr(pp, "_resolve_rag_store", lambda: _FakeStore())
    monkeypatch.setattr(pp, "_embed_query", lambda _t: None)  # text-only
    r = _run({"query": "anything", "k": 5})
    assert r["ok"] and r["access_scope"] == "public"
    paths = {x["source_path"] for x in r["results"]}
    assert "pub.md" in paths
    assert "sec.md" not in paths, "restricted chunk leaked through the MCP retrieve"


def test_no_store_is_fail_soft(monkeypatch):
    monkeypatch.setattr(pp, "_resolve_rag_store", lambda: None)
    r = _run({"query": "x"})
    assert r["ok"] is False and "No RAG store configured" in r["note"]


def test_local_store_without_tier_reporting_fails_closed(monkeypatch):
    monkeypatch.setattr(pp, "_resolve_rag_store", lambda: _NoTierStore())
    monkeypatch.setattr(pp, "_embed_query", lambda _t: None)
    r = _run({"query": "x"})
    assert r["ok"] is False and "fail-closed" in r["note"]
    assert not r["results"]


def test_empty_query_rejected():
    r = _run({"query": "   "})
    assert r["ok"] is False and "query" in r["error"]


def test_unknown_path_denied(monkeypatch):
    # access_tier_for returns None for an unindexed path -> treated as deny, not public.
    class _Store(_FakeStore):
        def access_tier_for(self, path):
            return None  # nothing known -> must be denied

    monkeypatch.setattr(pp, "_resolve_rag_store", lambda: _Store())
    monkeypatch.setattr(pp, "_embed_query", lambda _t: None)
    r = _run({"query": "x", "k": 5})
    assert r["ok"] and r["results"] == [], "unknown-tier chunks must be denied, not returned"
