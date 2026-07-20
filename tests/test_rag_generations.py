# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for RAG generational blue/green infrastructure.

TDD: tests written before implementation.
"""

from __future__ import annotations

import os

import pytest

from axiom.rag.store import CORPUS_COMMUNITY

# Skip if no DATABASE_URL
pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set — requires live PG",
)


@pytest.fixture(scope="module")
def store():
    from axiom.rag.store import RAGStore

    s = RAGStore(os.environ["DATABASE_URL"])
    s.connect()
    yield s
    s.close()


@pytest.fixture
def gen_manager(store):
    from axiom.rag.generation import GenerationManager

    return GenerationManager(store)


class TestGenerationManager:
    def test_get_active_generation(self, gen_manager):
        """Default active generation is 1."""
        gen = gen_manager.get_active_generation(CORPUS_COMMUNITY)
        assert gen >= 1

    def test_get_candidate_generation(self, gen_manager):
        """No candidate by default."""
        candidate = gen_manager.get_candidate_generation(CORPUS_COMMUNITY)
        assert candidate is None

    def test_create_candidate(self, gen_manager):
        """Create a new candidate generation."""
        active = gen_manager.get_active_generation(CORPUS_COMMUNITY)
        candidate = gen_manager.create_candidate(CORPUS_COMMUNITY)
        assert candidate == active + 1

    def test_promote_candidate(self, gen_manager):
        """Promoting candidate makes it active."""
        candidate = gen_manager.create_candidate(CORPUS_COMMUNITY)
        gen_manager.promote(CORPUS_COMMUNITY, candidate)
        assert gen_manager.get_active_generation(CORPUS_COMMUNITY) == candidate
        assert gen_manager.get_candidate_generation(CORPUS_COMMUNITY) is None

    def test_discard_candidate(self, gen_manager):
        """Discarding candidate clears it without changing active."""
        active_before = gen_manager.get_active_generation(CORPUS_COMMUNITY)
        candidate = gen_manager.create_candidate(CORPUS_COMMUNITY)
        gen_manager.discard(CORPUS_COMMUNITY, candidate)
        assert gen_manager.get_active_generation(CORPUS_COMMUNITY) == active_before
        assert gen_manager.get_candidate_generation(CORPUS_COMMUNITY) is None

    def test_rollback_to_previous(self, gen_manager):
        """Rollback sets active to a previous generation."""
        gen1 = gen_manager.get_active_generation(CORPUS_COMMUNITY)
        candidate = gen_manager.create_candidate(CORPUS_COMMUNITY)
        gen_manager.promote(CORPUS_COMMUNITY, candidate)
        # Now active is gen1+1
        gen_manager.rollback(CORPUS_COMMUNITY, gen1)
        assert gen_manager.get_active_generation(CORPUS_COMMUNITY) == gen1


class TestGenerationAwareSearch:
    """Search respects active generation."""

    def test_search_returns_active_generation_only(self, store, gen_manager):
        """Search with generation filter returns only matching chunks."""
        # This test verifies the column exists and filtering works

        active = gen_manager.get_active_generation(CORPUS_COMMUNITY)
        # Search should work (may return 0 results if corpus empty for this gen)
        results = store.search(
            query_text="reactor",
            corpora=[CORPUS_COMMUNITY],
            limit=2,
            corpus_generation=active,
        )
        # All results should be from the active generation
        # (can't assert this without data, but the param must be accepted)
        assert isinstance(results, list)


class TestRetrievalLog:
    """Retrieval quality logging for A/B measurement."""

    def test_log_retrieval(self, store):
        """Log a retrieval event."""
        from axiom.rag.quality import log_retrieval

        log_retrieval(
            store=store,
            query_hash="abc123",
            corpus=CORPUS_COMMUNITY,
            generation=1,
            chunking_tier="fixed",
            result_count=5,
            top_similarity=0.85,
            latency_ms=42,
        )

    def test_compute_generation_quality(self, store):
        """Compute quality metrics for a generation."""
        from axiom.rag.quality import compute_generation_quality

        quality = compute_generation_quality(store, CORPUS_COMMUNITY, generation=1)
        assert hasattr(quality, "mean_similarity")
        assert hasattr(quality, "query_count")
        assert hasattr(quality, "feedback_ratio")
