# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for the T0-1 retriever orchestrator.

The retriever composes:
    store.search (vector)  ─┐
    store.search (text)    ─┼─► RRF fuse ─► access filter ─► citation keys
                            ┘

Returns a list of ``RetrievedChunk`` with stable ``citation_key`` (C1, C2,
...) so the downstream prompt template and citation postprocessor can
verify inline markers deterministically.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from axiom.rag.retriever import AccessContext, RetrievedChunk, retrieve
from axiom.rag.store import SearchResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result(
    path: str,
    idx: int = 0,
    sim: float = 0.5,
    score: float = 0.5,
    corpus: str = "rag-internal",
    text: str | None = None,
) -> SearchResult:
    return SearchResult(
        source_path=path,
        source_title=path,
        chunk_text=text or f"text for {path}#{idx}",
        chunk_index=idx,
        similarity=sim,
        combined_score=score,
        corpus=corpus,
    )


def _store_with(vector: list[SearchResult], text: list[SearchResult]) -> MagicMock:
    """A mock store that returns ``vector`` for pure-vector calls and
    ``text`` for pure-text calls. The retriever distinguishes the two by
    whether ``query_text`` is non-empty AND ``query_embedding`` is None."""
    store = MagicMock()

    def _search(query_embedding=None, query_text="", **_):
        if query_embedding is None:
            return list(text)
        return list(vector)

    store.search.side_effect = _search
    return store


# ---------------------------------------------------------------------------
# Core behavior
# ---------------------------------------------------------------------------


class TestRetrieveBasic:
    def test_returns_retrieved_chunks_with_citation_keys(self):
        store = _store_with(
            vector=[_result("a.md", 0), _result("b.md", 0), _result("c.md", 0)],
            text=[_result("a.md", 0), _result("b.md", 0), _result("c.md", 0)],
        )
        chunks = retrieve(
            store=store,
            query_text="quantum",
            query_embedding=[0.1] * 8,
            limit=3,
        )
        assert len(chunks) == 3
        assert isinstance(chunks[0], RetrievedChunk)
        assert [c.citation_key for c in chunks] == ["C1", "C2", "C3"]

    def test_fused_ranking_prefers_docs_in_both_lists(self):
        # a leads vector, b leads text, a is also #2 in text, b is also #2 in vec.
        # a and b should both appear, with the better fused doc first.
        store = _store_with(
            vector=[_result("a.md"), _result("b.md"), _result("c.md")],
            text=[_result("b.md"), _result("a.md"), _result("d.md")],
        )
        chunks = retrieve(
            store=store,
            query_text="q",
            query_embedding=[0.0] * 4,
            limit=4,
        )
        ids = [c.source_path for c in chunks]
        # a and b rank higher than c and d.
        assert set(ids[:2]) == {"a.md", "b.md"}
        assert "c.md" in ids and "d.md" in ids

    def test_empty_rankings_returns_empty(self):
        store = _store_with(vector=[], text=[])
        assert retrieve(store=store, query_text="x", query_embedding=[0.0]) == []


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


class TestAccessFilter:
    def test_filter_drops_higher_tier_chunks(self):
        r1 = _result("pub.md")
        r2 = _result("course.md")
        # The store returns these but the retriever must filter.
        # We attach tier metadata via a sidecar dict keyed by source_path.
        tier_map = {"pub.md": "public", "course.md": "course"}
        store = _store_with(vector=[r1, r2], text=[r1, r2])
        ctx = AccessContext(max_access_tier="public")
        chunks = retrieve(
            store=store,
            query_text="q",
            query_embedding=[0.0] * 4,
            limit=5,
            access_context=ctx,
            tier_lookup=lambda path: tier_map.get(path, "public"),
        )
        assert [c.source_path for c in chunks] == ["pub.md"]

    def test_filter_allows_equal_tier(self):
        r = _result("course.md")
        store = _store_with(vector=[r], text=[r])
        ctx = AccessContext(max_access_tier="course")
        chunks = retrieve(
            store=store,
            query_text="q",
            query_embedding=[0.0] * 4,
            limit=5,
            access_context=ctx,
            tier_lookup=lambda _: "course",
        )
        assert len(chunks) == 1

    def test_citation_keys_dense_after_filter(self):
        """Citation keys must be dense (C1, C2) after filtering — never
        skip numbers because a filtered chunk would have been C2."""
        rs = [_result("a.md"), _result("b.md"), _result("c.md")]
        store = _store_with(vector=rs, text=rs)
        ctx = AccessContext(max_access_tier="public")
        chunks = retrieve(
            store=store,
            query_text="q",
            query_embedding=[0.0] * 4,
            limit=3,
            access_context=ctx,
            tier_lookup=lambda path: "course" if path == "b.md" else "public",
        )
        assert [c.citation_key for c in chunks] == ["C1", "C2"]
        assert [c.source_path for c in chunks] == ["a.md", "c.md"]


# ---------------------------------------------------------------------------
# Limit
# ---------------------------------------------------------------------------


class TestRetrieveLimit:
    def test_limit_truncates_post_fusion(self):
        rs = [_result(f"{i}.md") for i in range(10)]
        store = _store_with(vector=rs, text=rs)
        chunks = retrieve(
            store=store,
            query_text="q",
            query_embedding=[0.0] * 4,
            limit=4,
        )
        assert len(chunks) == 4
