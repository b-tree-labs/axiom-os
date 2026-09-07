# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""The chat agent's RAG context must embed the query for vector+keyword RRF.

Regression guard: ``_rag_context`` historically hardcoded
``query_embedding=None`` (text-only keyword retrieval), which gives poor
recall on semantic/paraphrased queries and silently degrades grounding. When
an embedder is configured, the query MUST be embedded so the retriever fuses
vector + keyword rankings.
"""

from __future__ import annotations

import axiom.rag.embeddings as emb
import axiom.rag.retriever as ret
from axiom.extensions.builtins.chat.agent import ChatAgent


class _FakeAgent:
    def __init__(self):
        self._last_retrieved = []

    def _get_rag_store(self):
        return object()  # non-None store


def test_rag_context_embeds_query_when_embedder_available(monkeypatch):
    captured = {}
    embed_calls: list[list[str]] = []

    def _embed(texts):
        embed_calls.append(texts)
        return [[0.25] * 768]

    monkeypatch.setattr(emb, "embed_texts", _embed)

    def fake_retrieve(store, query_text, query_embedding, corpora, limit):
        captured["embedding"] = query_embedding
        captured["query_text"] = query_text
        captured["corpora"] = corpora
        return []

    monkeypatch.setattr(ret, "retrieve", fake_retrieve)

    ChatAgent._rag_context(_FakeAgent(), "MSRE flush salt drained March 1965")

    assert captured["embedding"] is not None, "query was not embedded (text-only regression)"
    assert len(captured["embedding"]) == 768
    assert captured["query_text"] == "MSRE flush salt drained March 1965"
    assert embed_calls == [["MSRE flush salt drained March 1965"]]  # local store: embed once
    assert captured["corpora"] is None  # a default scope draws from every corpus


def test_rag_context_falls_back_to_text_only_without_embedder(monkeypatch):
    captured = {}
    monkeypatch.setattr(emb, "embed_texts", lambda texts: None)  # no provider

    def fake_retrieve(store, query_text, query_embedding, corpora, limit):
        captured["embedding"] = query_embedding
        return []

    monkeypatch.setattr(ret, "retrieve", fake_retrieve)
    ChatAgent._rag_context(_FakeAgent(), "anything")
    assert captured["embedding"] is None  # graceful keyword-only fallback


# ---------------------------------------------------------------------------
# A store that runs hybrid search server-side (the served retrieval store of
# a joined site node, ``does_own_hybrid = True``) takes the query as text.
# Embedding locally would put retrieval logic on the client and add a second
# round-trip; mirrors ``axiom_rag__retrieve``.
# ---------------------------------------------------------------------------


class _ServedStore:
    does_own_hybrid = True


class _FakeServedAgent(_FakeAgent):
    def _get_rag_store(self):
        return _ServedStore()


def test_rag_context_skips_local_embedding_when_store_owns_hybrid(monkeypatch):
    embed_calls: list[list[str]] = []

    def _embed(texts):
        embed_calls.append(texts)
        return [[0.25] * 768]  # a working embedder, deliberately: it must not be asked

    monkeypatch.setattr(emb, "embed_texts", _embed)

    captured = {}

    def fake_retrieve(store, query_text, query_embedding, corpora, limit):
        captured["store"] = store
        captured["embedding"] = query_embedding
        captured["query_text"] = query_text
        captured["corpora"] = corpora
        captured["limit"] = limit
        return []

    monkeypatch.setattr(ret, "retrieve", fake_retrieve)

    ChatAgent._rag_context(_FakeServedAgent(), "MSRE flush salt", limit=3)

    assert embed_calls == [], "served store: the query goes to the site as text, not embedded here"
    assert captured["embedding"] is None
    assert captured["query_text"] == "MSRE flush salt"
    assert captured["limit"] == 3
    assert isinstance(captured["store"], _ServedStore)
