# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for RAG generation infrastructure (no DB required)."""

from __future__ import annotations

from unittest.mock import MagicMock


class TestGenerationManagerUnit:
    def test_importable(self):
        from axiom.rag.generation import GenerationManager
        assert GenerationManager is not None

    def test_init_with_store(self):
        from axiom.rag.generation import GenerationManager
        mock_store = MagicMock()
        gm = GenerationManager(mock_store)
        assert gm._store is mock_store


class TestQualityUnit:
    def test_importable(self):
        from axiom.rag.quality import compute_generation_quality, log_retrieval
        assert callable(log_retrieval)
        assert callable(compute_generation_quality)

    def test_generation_quality_dataclass(self):
        from axiom.rag.quality import GenerationQuality
        q = GenerationQuality(
            corpus="rag-community",
            generation=1,
            query_count=100,
            mean_similarity=0.75,
            p50_similarity=0.78,
            feedback_ratio=0.6,
            mean_latency_ms=45,
        )
        assert q.query_count == 100
        assert q.mean_similarity == 0.75


class TestSchemaHasGeneration:
    def test_chunks_ddl_has_corpus_generation(self):
        """Schema DDL must include corpus_generation column."""
        from axiom.rag.store import _SCHEMA_SQL
        assert "corpus_generation" in _SCHEMA_SQL, (
            "chunks table DDL missing corpus_generation column"
        )

    def test_documents_ddl_has_corpus_generation(self):
        # Check documents table specifically
        import re

        from axiom.rag.store import _SCHEMA_SQL
        doc_match = re.search(
            r"CREATE TABLE.*?documents\s*\((.*?)\);",
            _SCHEMA_SQL, re.DOTALL | re.IGNORECASE,
        )
        assert doc_match, "Could not find documents CREATE TABLE"
        assert "corpus_generation" in doc_match.group(1), (
            "documents table missing corpus_generation column"
        )

    def test_documents_ddl_has_graph_extracted_at(self):
        import re

        from axiom.rag.store import _SCHEMA_SQL
        doc_match = re.search(
            r"CREATE TABLE.*?documents\s*\((.*?)\);",
            _SCHEMA_SQL, re.DOTALL | re.IGNORECASE,
        )
        assert "graph_extracted_at" in doc_match.group(1), (
            "documents table missing graph_extracted_at column"
        )

    def test_search_accepts_corpus_generation(self):
        """store.search() must accept corpus_generation parameter."""
        import inspect

        from axiom.rag.store import RAGStore
        sig = inspect.signature(RAGStore.search)
        assert "corpus_generation" in sig.parameters, (
            "RAGStore.search() missing corpus_generation parameter"
        )
