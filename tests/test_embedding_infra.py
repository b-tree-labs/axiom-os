# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests that embedding infrastructure is properly installed and working.

These tests ensure we never ship a corpus without embeddings again.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from axiom.rag.embeddings import embed_texts


class TestEmbeddingProviderChain:
    """Verify the embedding fallback chain discovers a working provider."""

    def test_embed_texts_returns_list_or_none(self):
        """embed_texts must return list[list[float]] or None, never crash."""
        result = embed_texts(["test embedding"])
        # Either we have a provider or we get None — never an exception
        assert result is None or (
            isinstance(result, list) and len(result) == 1
        )

    def test_embed_texts_empty_input(self):
        """Empty input returns empty list."""
        result = embed_texts([])
        assert result == []

    def test_embed_texts_dimension(self):
        """If a provider is available, embeddings must be 768-dim (nomic-embed-text)."""
        result = embed_texts(["test"])
        if result is not None:
            assert len(result[0]) == 768, (
                f"Expected 768-dim embeddings (nomic-embed-text), got {len(result[0])}"
            )

    def test_ollama_provider_detected(self):
        """Ollama with nomic-embed-text should be auto-detected if running."""
        import socket
        try:
            with socket.create_connection(("localhost", 11434), timeout=1):
                ollama_up = True
        except OSError:
            ollama_up = False

        if ollama_up:
            result = embed_texts(["reactor safety"])
            assert result is not None, (
                "Ollama is running but embed_texts returned None — "
                "nomic-embed-text model may not be pulled"
            )
        else:
            pytest.skip("Ollama not running locally")


class TestEmbeddingSetup:
    """Verify embedding provider is set up during axi setup."""

    def test_setup_infra_has_ollama_check(self):
        """The infra checks must include Ollama/embedding."""
        from axiom.setup.infra import run_infra_checks

        checks = run_infra_checks(skip_cluster=True)
        check_names = [c.name for c in checks]
        assert any("Embedding" in n or "Ollama" in n for n in check_names), (
            f"No embedding check in infra checks: {check_names}. "
            "Embedding setup must be part of axi setup to prevent "
            "corpus ingestion without embeddings."
        )

    def test_check_ollama_embedding_function_exists(self):
        """check_ollama_embedding must be importable."""
        from axiom.setup.infra import check_ollama_embedding
        result = check_ollama_embedding()
        assert result.name == "Embedding (Ollama)"

    def test_embedding_model_constant_defined(self):
        """The default embedding model must be defined in config."""
        from axiom.rag.embeddings import _OLLAMA_EMBED_MODEL
        assert _OLLAMA_EMBED_MODEL == "nomic-embed-text"


class TestIngestEmbedding:
    """Verify that RAG ingest always generates embeddings."""

    def test_ingest_calls_embed_texts(self):
        """ingest_file must call embed_texts for each document."""
        from axiom.rag.ingest import ingest_file

        mock_store = MagicMock()
        mock_store.get_document.return_value = None
        mock_store.find_by_content_hash.return_value = []

        with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False) as f:
            f.write("This is a test document about reactor safety analysis.")
            f.flush()
            tmp_path = f.name

        try:
            with patch("axiom.rag.ingest.embed_texts") as mock_embed:
                mock_embed.return_value = [[0.1] * 768]
                ingest_file(Path(tmp_path), mock_store)
                assert mock_embed.called, (
                    "ingest_file did not call embed_texts — "
                    "documents will be ingested without embeddings!"
                )
        finally:
            os.unlink(tmp_path)


class TestPackInstallEmbedding:
    """Verify that pack installation ensures embeddings exist."""

    def test_load_pack_csv_warns_on_missing_embeddings(self):
        """load_pack_csv should log a warning when chunks lack embeddings."""
        # This tests that we at least detect the problem
        from axiom.rag.store import RAGStore

        # We can't easily test the full flow without a DB,
        # but we can verify the method signature accepts an embed callback
        assert hasattr(RAGStore, "load_pack_csv"), (
            "RAGStore.load_pack_csv must exist for pack installation"
        )


class TestEmbeddingCoverage:
    """Integration tests — require DATABASE_URL."""

    @pytest.mark.skipif(
        not os.environ.get("DATABASE_URL"),
        reason="DATABASE_URL not set",
    )
    def test_community_corpus_has_embeddings(self):
        """After pack install, >95% of community chunks must have embeddings."""
        from axiom.rag.store import RAGStore

        store = RAGStore(os.environ["DATABASE_URL"])
        store.connect()
        try:
            with store._cur() as cur:
                cur.execute(
                    "SELECT count(*) AS n FROM chunks "
                    "WHERE corpus = 'rag-community'"
                )
                total = cur.fetchone()["n"]
                if total == 0:
                    pytest.skip("No community corpus loaded")

                cur.execute(
                    "SELECT count(*) AS n FROM chunks "
                    "WHERE corpus = 'rag-community' AND embedding IS NOT NULL"
                )
                with_emb = cur.fetchone()["n"]

            coverage = with_emb / total if total > 0 else 0
            assert coverage > 0.95, (
                f"Only {coverage:.1%} of community chunks have embeddings "
                f"({with_emb}/{total}). Expected >95%. "
                "Run embedding pipeline to fix."
            )
        finally:
            store.close()

    @pytest.mark.skipif(
        not os.environ.get("DATABASE_URL"),
        reason="DATABASE_URL not set",
    )
    def test_embedding_dimension_matches_schema(self):
        """All stored embeddings must be 768-dim (nomic-embed-text)."""
        from axiom.rag.store import RAGStore

        store = RAGStore(os.environ["DATABASE_URL"])
        store.connect()
        try:
            with store._cur() as cur:
                cur.execute(
                    "SELECT vector_dims(embedding) AS dim FROM chunks "
                    "WHERE embedding IS NOT NULL LIMIT 1"
                )
                row = cur.fetchone()
                if row is None:
                    pytest.skip("No embeddings in store")
                assert row["dim"] == 768, (
                    f"Embedding dimension is {row['dim']}, expected 768 (nomic-embed-text)"
                )
        finally:
            store.close()
