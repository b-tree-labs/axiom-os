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
    monkeypatch.setattr(emb, "embed_texts", lambda texts: [[0.25] * 768])

    def fake_retrieve(store, query_text, query_embedding, limit):
        captured["embedding"] = query_embedding
        captured["query_text"] = query_text
        return []

    monkeypatch.setattr(ret, "retrieve", fake_retrieve)

    ChatAgent._rag_context(_FakeAgent(), "MSRE flush salt drained March 1965")

    assert captured["embedding"] is not None, "query was not embedded (text-only regression)"
    assert len(captured["embedding"]) == 768
    assert captured["query_text"] == "MSRE flush salt drained March 1965"


def test_rag_context_falls_back_to_text_only_without_embedder(monkeypatch):
    captured = {}
    monkeypatch.setattr(emb, "embed_texts", lambda texts: None)  # no provider

    def fake_retrieve(store, query_text, query_embedding, limit):
        captured["embedding"] = query_embedding
        return []

    monkeypatch.setattr(ret, "retrieve", fake_retrieve)
    ChatAgent._rag_context(_FakeAgent(), "anything")
    assert captured["embedding"] is None  # graceful keyword-only fallback
