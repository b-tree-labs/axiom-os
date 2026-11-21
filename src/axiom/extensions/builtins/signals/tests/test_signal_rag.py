# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Signal RAG system."""

import pytest

from axiom.extensions.builtins.signals.signal_rag import (
    KeywordEmbeddings,
    RetrievalResult,
    SignalChunk,
    SignalRAG,
    VectorStore,
)


class TestSignalChunk:
    """Test SignalChunk dataclass."""

    def test_construction(self):
        chunk = SignalChunk(
            signal_id="abc123",
            text="Kevin is working on data pipeline optimization",
            timestamp="2026-02-15T10:00:00Z",
            signal_type="progress",
            initiative="Project Alpha",
            source="voice",
        )

        assert chunk.signal_id == "abc123"
        assert "Kevin" in chunk.text
        assert chunk.embedding is None  # Not set yet

    def test_roundtrip(self):
        chunk = SignalChunk(
            signal_id="def456",
            text="Test chunk",
            timestamp="2026-02-15T10:00:00Z",
            signal_type="blocker",
            initiative="MSR",
            source="email",
            metadata={"originator": "ben@example.com"},
        )

        d = chunk.to_dict()
        restored = SignalChunk.from_dict(d)

        assert restored.signal_id == chunk.signal_id
        assert restored.metadata == chunk.metadata


class TestKeywordEmbeddings:
    """Test the fallback keyword-based embedding provider."""

    @pytest.fixture
    def embedder(self):
        return KeywordEmbeddings()

    def test_is_always_available(self, embedder):
        """Keyword embeddings should always be available (no API needed)."""
        assert embedder.is_available() is True

    def test_produces_embeddings(self, embedder):
        texts = ["data pipeline architecture"]
        embeddings = embedder.embed(texts)

        assert len(embeddings) == 1
        assert isinstance(embeddings[0], list)
        assert len(embeddings[0]) > 0

    def test_similar_texts_have_similar_embeddings(self, embedder):
        text1 = "data pipeline optimization architecture"
        text2 = "pipeline optimization data processing"
        text3 = "catering lunch menu friday"

        emb1 = embedder.embed([text1])[0]
        emb2 = embedder.embed([text2])[0]
        emb3 = embedder.embed([text3])[0]

        # Compute simple dot product similarity
        def dot(a, b):
            return sum(x*y for x, y in zip(a, b))

        sim_12 = dot(emb1, emb2)
        sim_13 = dot(emb1, emb3)

        # text1 and text2 should be at least as similar as text1 and text3
        # (keyword embedder is low-dimensional, so ties are expected)
        assert sim_12 >= sim_13

    def test_empty_text(self, embedder):
        embeddings = embedder.embed([""])
        assert len(embeddings) == 1


class TestVectorStore:
    """Test the VectorStore class."""

    @pytest.fixture
    def store(self, tmp_path):
        s = VectorStore()
        s._test_index_path = tmp_path / "index.json"  # type: ignore[attr-defined]
        s._test_embeddings_path = tmp_path / "embeddings.json"  # type: ignore[attr-defined]
        return s

    @pytest.fixture
    def embedder(self):
        return KeywordEmbeddings()

    @pytest.fixture
    def sample_chunks(self):
        return [
            SignalChunk(
                signal_id="sig1",
                text="Kevin working on data pipeline optimization code",
                timestamp="2026-02-15T10:00:00Z",
                signal_type="progress",
                initiative="Project Alpha",
                source="voice",
            ),
            SignalChunk(
                signal_id="sig2",
                text="MSR salt chemistry experiments ongoing",
                timestamp="2026-02-15T11:00:00Z",
                signal_type="progress",
                initiative="MSR Project",
                source="voice",
            ),
            SignalChunk(
                signal_id="sig3",
                text="Blocked on NRC approval for license",
                timestamp="2026-02-15T12:00:00Z",
                signal_type="blocker",
                initiative="Project Alpha",
                source="email",
            ),
        ]

    def _add_chunks(self, store, chunks, embedder, query_text=None):
        """Embed all chunks (and optional query) together for consistent vocab.

        KeywordEmbeddings builds vocabulary per-call, so everything must
        be embedded in one call to produce comparable vectors.
        Returns the query embedding if query_text was provided.
        """
        texts = [c.text for c in chunks]
        if query_text:
            texts.append(query_text)
        all_embs = embedder.embed(texts)
        for chunk, emb in zip(chunks, all_embs[: len(chunks)]):
            store.add(chunk, emb)
        return all_embs[-1] if query_text else None

    def test_add_chunks(self, store, sample_chunks, embedder):
        self._add_chunks(store, sample_chunks, embedder)
        assert len(store.chunks) == 3

    def test_search_returns_results(self, store, sample_chunks, embedder):
        query_emb = self._add_chunks(
            store, sample_chunks, embedder, query_text="data pipeline"
        )
        results = store.search(query_emb, top_k=2)

        assert len(results) <= 2
        assert isinstance(results[0], tuple)  # (chunk, score)

    def test_search_relevance_ordering(self, store, sample_chunks, embedder):
        query_emb = self._add_chunks(
            store, sample_chunks, embedder, query_text="data pipeline optimization"
        )
        results = store.search(query_emb, top_k=3)

        # First result should be most relevant
        chunk, _score = results[0]
        assert "pipeline" in chunk.text or "data" in chunk.text

    def test_persistence(self, store, sample_chunks, embedder, tmp_path):
        self._add_chunks(store, sample_chunks, embedder)

        idx_path = tmp_path / "index.json"
        emb_path = tmp_path / "embeddings.json"
        store.save(idx_path, emb_path)

        # Create new store from same paths
        store2 = VectorStore()
        loaded = store2.load(idx_path, emb_path)

        assert loaded is True
        assert len(store2.chunks) == 3

    def test_clear(self, store, sample_chunks, embedder):
        self._add_chunks(store, sample_chunks, embedder)
        assert len(store.chunks) == 3

        store.chunks.clear()
        store.embeddings.clear()
        assert len(store.chunks) == 0


class TestSignalRAG:
    """Test the SignalRAG class."""

    @pytest.fixture
    def rag(self, tmp_path):
        return SignalRAG(
            index_path=tmp_path / "index.json",
            embeddings_path=tmp_path / "embeddings.json",
        )

    @pytest.fixture
    def rag_with_signals(self, tmp_path):
        signals = [
            {
                "signal_id": "sig_voice_1",
                "source": "voice",
                "timestamp": "2026-02-15T10:00:00Z",
                "raw_text": "Kevin making progress on data pipeline optimization",
                "detail": "Pipeline progress",
                "people": ["Kevin"],
                "initiative": "Project Alpha",
                "signal_type": "progress",
                "confidence": 0.8,
                "metadata": {},
            },
            {
                "signal_id": "sig_voice_2",
                "source": "voice",
                "timestamp": "2026-02-15T11:00:00Z",
                "raw_text": "MSR salt loop experiments complete",
                "detail": "MSR experiments done",
                "people": [],
                "initiative": "MSR Project",
                "signal_type": "progress",
                "confidence": 0.8,
                "metadata": {},
            },
        ]

        rag = SignalRAG(
            index_path=tmp_path / "index.json",
            embeddings_path=tmp_path / "embeddings.json",
        )
        # Force keyword embedder so tests don't depend on openai package
        rag.embedder = KeywordEmbeddings()
        rag.index_signals(signals)
        return rag

    def test_index_signals(self, rag_with_signals):
        assert len(rag_with_signals.store.chunks) >= 2

    def test_query_returns_results(self, rag_with_signals):
        results = rag_with_signals.query("data pipeline")

        assert len(results) >= 1

    def test_query_with_filters(self, rag_with_signals):
        results = rag_with_signals.query(
            "progress",
            category_filter="Project Alpha",
        )

        for r in results:
            assert "Alpha" in r.chunk.initiative

    def test_signal_to_chunk_conversion(self, rag):
        signal_dict = {
            "signal_id": "test_sig",
            "source": "voice",
            "timestamp": "2026-02-15T10:00:00Z",
            "raw_text": "Raw transcript here",
            "detail": "Extracted detail",
            "people": ["Kevin", "Ben"],
            "initiatives": ["Alpha"],
            "signal_type": "progress",
            "confidence": 0.8,
            "metadata": {},
        }

        chunk = rag._signal_to_chunk(signal_dict)

        assert chunk.signal_id == "test_sig"
        assert "Kevin" in chunk.text or "detail" in chunk.text.lower()


class TestRetrievalResult:
    """Test RetrievalResult dataclass."""

    def test_construction(self):
        chunk = SignalChunk(
            signal_id="abc",
            text="test",
            timestamp="2026-02-15T10:00:00Z",
            signal_type="progress",
            initiative="Alpha",
            source="voice",
        )

        result = RetrievalResult(
            chunk=chunk,
            relevance=0.85,
        )

        assert result.relevance == 0.85
        assert result.chunk.signal_id == "abc"
