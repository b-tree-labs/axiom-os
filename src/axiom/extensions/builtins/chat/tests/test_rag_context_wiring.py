# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""T0-1 integration test: ChatAgent._rag_context uses the new retriever.

The body of ``_rag_context`` was swapped to call
``axiom.rag.retriever.retrieve`` + ``build_rag_context_block`` so the
model sees stable ``[C<n>]`` markers. These tests exercise the wiring
with a mock store — no DB required.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.chat.agent import ChatAgent
from axiom.rag.store import SearchResult


def _result(path: str, idx: int = 0) -> SearchResult:
    return SearchResult(
        source_path=path,
        source_title=path,
        chunk_text=f"body of {path}",
        chunk_index=idx,
        similarity=0.7,
        combined_score=0.7,
        corpus="rag-internal",
    )


@pytest.fixture
def agent_with_store():
    """Construct a ChatAgent and inject a mock RAG store directly."""
    # Bypass __init__'s heavier setup by making a bare instance.
    agent = ChatAgent.__new__(ChatAgent)
    agent._rag_init_attempted = True
    agent._last_retrieved = []
    store = MagicMock()
    store.search.side_effect = lambda query_embedding=None, query_text="", **_: (
        [] if query_embedding is not None else
        [_result("a.md"), _result("b.md"), _result("c.md")]
    )
    agent._rag_store = store
    return agent


class TestRagContextFormat:
    def test_block_has_citation_markers(self, agent_with_store):
        out = agent_with_store._rag_context("quantum", limit=3)
        assert "[C1]" in out
        assert "[C2]" in out
        assert "[C3]" in out

    def test_block_includes_cite_guidance(self, agent_with_store):
        out = agent_with_store._rag_context("quantum", limit=3)
        assert "cite" in out.lower() or "citation" in out.lower()

    def test_stashes_retrieved_for_postprocessing(self, agent_with_store):
        agent_with_store._rag_context("quantum", limit=3)
        assert len(agent_with_store._last_retrieved) == 3
        assert agent_with_store._last_retrieved[0].citation_key == "C1"


class TestEmptyCases:
    def test_no_store_returns_empty(self):
        agent = ChatAgent.__new__(ChatAgent)
        agent._rag_init_attempted = True
        agent._rag_store = None
        agent._last_retrieved = ["stale"]  # must be cleared
        assert agent._rag_context("q") == ""
        assert agent._last_retrieved == []

    def test_empty_query_returns_empty(self, agent_with_store):
        assert agent_with_store._rag_context("   ") == ""

    def test_no_results_returns_empty(self):
        agent = ChatAgent.__new__(ChatAgent)
        agent._rag_init_attempted = True
        agent._last_retrieved = []
        store = MagicMock()
        store.search.return_value = []
        agent._rag_store = store
        assert agent._rag_context("quantum") == ""


class TestLowConfidenceHint:
    def test_low_similarity_appends_hint(self):
        agent = ChatAgent.__new__(ChatAgent)
        agent._rag_init_attempted = True
        agent._last_retrieved = []
        store = MagicMock()
        weak = SearchResult(
            source_path="weak.md", source_title="weak.md",
            chunk_text="hardly relevant", chunk_index=0,
            similarity=0.05, combined_score=0.05, corpus="rag-internal",
        )
        store.search.side_effect = lambda query_embedding=None, query_text="", **_: (
            [] if query_embedding is not None else [weak]
        )
        agent._rag_store = store
        out = agent._rag_context("quantum")
        assert "[C1]" in out
        assert "Low RAG confidence" in out
