# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for SQLiteRAGStore — SQLite+FTS5+sqlite-vec backend for RAG."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from axiom.rag.chunker import Chunk
from axiom.rag.store import CORPUS_COMMUNITY, CORPUS_INTERNAL, CORPUS_ORG, SearchResult


@pytest.fixture
def store():
    from axiom.rag.sqlite_store import SQLiteRAGStore

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test_rag.db"
        s = SQLiteRAGStore(f"sqlite:///{db_path}")
        s.connect()
        yield s
        s.close()


@pytest.fixture
def sample_chunks() -> list[Chunk]:
    return [
        Chunk(
            source_path="doc1.pdf",
            source_title="Document One",
            source_type="pdf",
            text="The thermal conductivity of UO2 decreases with temperature.",
            chunk_index=0,
            start_line=1,
        ),
        Chunk(
            source_path="doc1.pdf",
            source_title="Document One",
            source_type="pdf",
            text="At 1200K, UO2 thermal conductivity is approximately 2.8 W/m-K.",
            chunk_index=1,
            start_line=5,
        ),
        Chunk(
            source_path="doc2.pdf",
            source_title="NRC Regulations",
            source_type="pdf",
            text="Emergency core cooling systems must meet the requirements of 10 CFR 50.46.",
            chunk_index=0,
            start_line=1,
        ),
    ]


@pytest.fixture
def sample_embeddings() -> list[list[float]]:
    import random
    random.seed(42)
    return [[random.uniform(-1, 1) for _ in range(768)] for _ in range(3)]


class TestConnect:
    def test_connect_creates_tables(self, store):
        stats = store.stats()
        assert stats["total_documents"] == 0
        assert stats["total_chunks"] == 0

    def test_connect_idempotent(self, store):
        store.connect()
        store.connect()
        assert store.stats()["total_chunks"] == 0


class TestWrite:
    def test_upsert_chunks(self, store, sample_chunks):
        store.upsert_chunks(sample_chunks, corpus=CORPUS_INTERNAL)
        assert store.stats()["total_chunks"] == 3

    def test_upsert_with_embeddings(self, store, sample_chunks, sample_embeddings):
        store.upsert_chunks(
            sample_chunks, embeddings=sample_embeddings, corpus=CORPUS_INTERNAL
        )
        assert store.stats()["total_chunks"] == 3

    def test_upsert_replaces_same_path_corpus(self, store, sample_chunks):
        store.upsert_chunks(sample_chunks[:2], corpus=CORPUS_INTERNAL)
        assert store.stats()["total_chunks"] == 2
        new_chunks = [
            Chunk(source_path="doc1.pdf", source_title="Updated",
                  source_type="pdf", text="New content.", chunk_index=0, start_line=1)
        ]
        store.upsert_chunks(new_chunks, corpus=CORPUS_INTERNAL)
        assert store.stats()["total_chunks"] == 1

    def test_delete_document(self, store, sample_chunks):
        store.upsert_chunks(sample_chunks, corpus=CORPUS_INTERNAL)
        store.delete_document("doc1.pdf", corpus=CORPUS_INTERNAL)
        assert store.stats()["total_chunks"] == 1

    def test_delete_corpus(self, store, sample_chunks):
        store.upsert_chunks(sample_chunks, corpus=CORPUS_COMMUNITY)
        deleted = store.delete_corpus(CORPUS_COMMUNITY)
        assert deleted == 3
        assert store.stats()["total_chunks"] == 0


class TestRead:
    def test_get_document(self, store, sample_chunks):
        store.upsert_chunks(sample_chunks[:2], corpus=CORPUS_INTERNAL, checksum="abc")
        doc = store.get_document("doc1.pdf", corpus=CORPUS_INTERNAL)
        assert doc is not None
        assert doc["source_path"] == "doc1.pdf"
        assert doc["chunk_count"] == 2

    def test_get_document_not_found(self, store):
        assert store.get_document("nonexistent.pdf") is None

    def test_find_by_content_hash(self, store, sample_chunks):
        store.upsert_chunks(sample_chunks[:2], corpus=CORPUS_INTERNAL, content_hash="hash123")
        results = store.find_by_content_hash("hash123")
        assert len(results) == 1
        assert results[0]["content_hash"] == "hash123"

    def test_find_by_content_hash_empty(self, store):
        assert store.find_by_content_hash("") == []


class TestSearch:
    def test_fulltext_search(self, store, sample_chunks):
        store.upsert_chunks(sample_chunks, corpus=CORPUS_COMMUNITY)
        results = store.search(query_text="thermal conductivity", limit=5)
        assert len(results) >= 1
        assert any("thermal" in r.chunk_text.lower() for r in results)

    def test_fulltext_search_no_results(self, store, sample_chunks):
        store.upsert_chunks(sample_chunks, corpus=CORPUS_COMMUNITY)
        results = store.search(query_text="quantum chromodynamics", limit=5)
        assert len(results) == 0

    def test_vector_search(self, store, sample_chunks, sample_embeddings):
        store.upsert_chunks(
            sample_chunks, embeddings=sample_embeddings, corpus=CORPUS_COMMUNITY
        )
        if not store._vec_available:
            pytest.skip("sqlite-vec not installed — vector search unavailable")
        results = store.search(query_embedding=sample_embeddings[0], limit=2)
        assert len(results) >= 1

    def test_hybrid_search(self, store, sample_chunks, sample_embeddings):
        store.upsert_chunks(
            sample_chunks, embeddings=sample_embeddings, corpus=CORPUS_COMMUNITY
        )
        results = store.search(
            query_embedding=sample_embeddings[0],
            query_text="thermal conductivity",
            limit=3,
        )
        assert len(results) >= 1

    def test_search_respects_corpus_filter(self, store, sample_chunks):
        store.upsert_chunks(sample_chunks[:2], corpus=CORPUS_COMMUNITY)
        store.upsert_chunks(sample_chunks[2:], corpus=CORPUS_ORG)
        results = store.search(query_text="emergency", corpora=[CORPUS_COMMUNITY], limit=5)
        assert all(r.corpus == CORPUS_COMMUNITY for r in results)

    def test_search_respects_limit(self, store, sample_chunks):
        store.upsert_chunks(sample_chunks, corpus=CORPUS_COMMUNITY)
        results = store.search(query_text="the", limit=1)
        assert len(results) <= 1

    def test_search_returns_search_result_type(self, store, sample_chunks):
        store.upsert_chunks(sample_chunks, corpus=CORPUS_COMMUNITY)
        results = store.search(query_text="thermal", limit=1)
        if results:
            r = results[0]
            assert isinstance(r, SearchResult)
            assert isinstance(r.combined_score, float)
            assert isinstance(r.similarity, float)


class TestStats:
    def test_stats_empty(self, store):
        stats = store.stats()
        assert stats["total_documents"] == 0
        assert stats["total_chunks"] == 0
        assert stats["chunks_by_corpus"] == {}
        assert stats["documents_by_corpus"] == {}

    def test_stats_with_data(self, store, sample_chunks):
        store.upsert_chunks(sample_chunks[:2], corpus=CORPUS_COMMUNITY)
        store.upsert_chunks(sample_chunks[2:], corpus=CORPUS_ORG)
        stats = store.stats()
        assert stats["total_chunks"] == 3
        assert stats["chunks_by_corpus"][CORPUS_COMMUNITY] == 2
        assert stats["chunks_by_corpus"][CORPUS_ORG] == 1


class TestStoreFactory:
    def test_create_store_postgres(self):
        from axiom.rag.store import RAGStore
        from axiom.rag.store_factory import create_store
        store = create_store("postgresql://user:pass@localhost/db")
        assert isinstance(store, RAGStore)

    def test_create_store_sqlite(self):
        from axiom.rag.sqlite_store import SQLiteRAGStore
        from axiom.rag.store_factory import create_store
        with tempfile.TemporaryDirectory() as tmp:
            store = create_store(f"sqlite:///{tmp}/test.db")
            assert isinstance(store, SQLiteRAGStore)

    def test_create_store_invalid_scheme(self):
        from axiom.rag.store_factory import create_store
        with pytest.raises(ValueError, match="Unsupported"):
            create_store("mysql://localhost/db")
