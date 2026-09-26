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

    def test_honors_chunk_classification_when_no_lookup(self):
        """Without an explicit lookup, retrieve enforces the chunk's OWN access
        metadata (projected by the store). A classified chunk is dropped under
        the fail-closed default context; a public one passes. Before this, the
        no-lookup fallback was a blanket 'unclassified' and the stored access
        columns were inert at read time."""
        pub = SearchResult(source_path="pub.md", source_title="pub.md",
                           chunk_text="public", chunk_index=0, similarity=0.9,
                           combined_score=0.9)
        secret = SearchResult(source_path="secret.md", source_title="secret.md",
                              chunk_text="secret", chunk_index=0, similarity=0.9,
                              combined_score=0.9, classification="classified")
        store = _store_with(vector=[pub, secret], text=[pub, secret])
        chunks = retrieve(store=store, query_text="q", query_embedding=[0.0] * 4,
                          limit=5, access_context=AccessContext())
        assert [c.source_path for c in chunks] == ["pub.md"]

    def test_honors_chunk_tier_when_no_lookup(self):
        inst = SearchResult(source_path="inst.md", source_title="inst.md",
                            chunk_text="x", chunk_index=0, similarity=0.9,
                            combined_score=0.9, access_tier="institutional")
        store = _store_with(vector=[inst], text=[inst])
        chunks = retrieve(store=store, query_text="q", query_embedding=[0.0] * 4,
                          limit=5, access_context=AccessContext())
        assert chunks == []

    def test_none_context_is_unfiltered_even_for_classified(self):
        """access_context=None means 'no filter' (the internal/admin path). This
        documents WHY the serving surfaces must pass a real context: passing
        None returns even classified chunks."""
        secret = SearchResult(source_path="secret.md", source_title="secret.md",
                              chunk_text="s", chunk_index=0, similarity=0.9,
                              combined_score=0.9, classification="classified")
        store = _store_with(vector=[secret], text=[secret])
        chunks = retrieve(store=store, query_text="q", query_embedding=[0.0] * 4,
                          limit=5, access_context=None)
        assert [c.source_path for c in chunks] == ["secret.md"]


class TestAccessContextResolver:
    def test_none_principal_is_failclosed_baseline(self):
        from axiom.rag.retriever import access_context_for_principal
        ctx = access_context_for_principal(None)
        assert ctx.max_access_tier == "public"
        assert ctx.allowed_classifications == frozenset({"unclassified"})
        assert ctx.site is None

    def test_site_scoping_off_by_default(self):
        from axiom.rag.retriever import access_context_for_principal
        # Even with a tenant-bearing handle, site stays None unless enabled —
        # the unattributed-corpus safety guard.
        assert access_context_for_principal("@alice:siteX").site is None

    def test_site_scoping_derives_site_from_handle(self):
        from axiom.rag.retriever import access_context_for_principal
        assert access_context_for_principal("@alice:siteX", site_scoping=True).site == "siteX"

    def test_site_scoping_handle_without_context_is_none(self):
        from axiom.rag.retriever import access_context_for_principal
        assert access_context_for_principal("@alice", site_scoping=True).site is None


class TestRerank:
    """The heuristic reranker (folded from hybrid.py) reorders fused candidates
    by query-term coverage / value-intent / authority, so it no longer runs
    nowhere. Default on; a no-content query and rerank=False are both no-ops."""

    # RRF ranks B first (position 1 in both rankings); A is the on-topic chunk.
    A = _result("a.md", text="alpha beta gamma delta")   # covers the query terms
    B = _result("b.md", text="wholly generic unrelated prose")  # covers nothing

    def _run(self, rerank):
        store = _store_with(vector=[self.B, self.A], text=[self.B, self.A])
        return retrieve(
            store=store, query_text="alpha beta", query_embedding=[0.0] * 4,
            limit=5, rerank=rerank,
        )

    def test_rerank_promotes_the_high_coverage_chunk(self):
        chunks = self._run(rerank=True)
        # A wins despite B's higher RRF rank — coverage (weight 3.0) dominates.
        assert [c.source_path for c in chunks] == ["a.md", "b.md"]

    def test_rerank_false_preserves_rrf_order(self):
        chunks = self._run(rerank=False)
        # Without rerank, RRF order stands: B (ranked first) leads.
        assert [c.source_path for c in chunks] == ["b.md", "a.md"]

    def test_no_content_query_is_a_noop(self):
        # A stopword-only query has no content terms -> rerank leaves RRF order.
        store = _store_with(vector=[self.B, self.A], text=[self.B, self.A])
        chunks = retrieve(store=store, query_text="the of and",
                          query_embedding=[0.0] * 4, limit=5, rerank=True)
        assert [c.source_path for c in chunks] == ["b.md", "a.md"]

    def test_citation_keys_dense_after_rerank(self):
        chunks = self._run(rerank=True)
        assert [c.citation_key for c in chunks] == ["C1", "C2"]


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
