# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the student-side local classroom index.

Phase 4 of the materials-flow tier. After a student syncs materials
(Phase 3), they need a queryable index — both vector-based (semantic
similarity) and graph-based (structure + entity relationships) so
Phase 6's ask command can ground answers in the class's own content.

Design:
- One SQLite DB per classroom at ``~/.axi/classrooms/<id>/index.db``
- Chunks + FTS5 keyword index + optional vector embeddings
  (sqlite-vec, if installed; degrades gracefully)
- Graph entities + edges extracted by the deterministic extractor
  (``axiom.graph.extractors.deterministic.extract_from_document``),
  no LLM required at ingest
- Hybrid search returns chunks with neighbor-context from the graph
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.classroom.classroom_local_index import (
    ClassroomLocalIndex,
)

# ---------------------------------------------------------------------------
# Embedding stub — fake but deterministic
# ---------------------------------------------------------------------------


def _fake_embed(texts: list[str]) -> list[list[float]]:
    """Returns a deterministic 4-dim vector per text based on character counts.

    Good enough to let the vector table exercise its paths without
    shipping a real model into the test suite.
    """
    import hashlib
    vectors = []
    for t in texts:
        h = hashlib.sha256(t.encode("utf-8")).digest()
        # Take 4 byte groups, map to [0,1].
        vectors.append([h[i] / 255.0 for i in range(4)])
    return vectors


@pytest.fixture
def index(tmp_path):
    idx = ClassroomLocalIndex(base_dir=tmp_path / "idx")
    idx.open()
    try:
        yield idx
    finally:
        idx.close()


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


class TestIngest:
    def test_ingest_persists_across_reopens(self, tmp_path):
        idx1 = ClassroomLocalIndex(base_dir=tmp_path / "idx")
        idx1.open()
        idx1.ingest(
            file_id="abc",
            title="Chapter 1",
            content="Fission splits heavy nuclei into lighter fragments.",
            embed=_fake_embed,
        )
        idx1.close()

        idx2 = ClassroomLocalIndex(base_dir=tmp_path / "idx")
        idx2.open()
        try:
            hits = idx2.search("fission", k=3)
            assert len(hits) >= 1
            assert any("fission" in h.text.lower() for h in hits)
        finally:
            idx2.close()

    def test_ingest_stores_multiple_chunks_for_long_content(self, index):
        # Semantic chunker (axiom.rag.semantic_chunker) keeps content whole
        # under ~2000 chars. Use enough material to exceed that and force a
        # split — this matches what real lecture / textbook sections look like.
        para = (
            "Reactor physics involves the study of neutron behavior in "
            "fission systems. Cross-sections, multiplication factors, and "
            "moderator-fuel interactions all play essential roles in "
            "reactor design and operation. " * 3
        )
        long_text = "\n\n".join(f"## Section {i}\n\n{para}" for i in range(20))
        index.ingest(
            file_id="long",
            title="Long doc",
            content=long_text,
            embed=_fake_embed,
        )
        # Long text should produce more than one chunk in the index.
        count = index.chunk_count()
        assert count > 1

    def test_reingest_replaces_prior_chunks(self, index):
        index.ingest(
            file_id="f1", title="T",
            content="original content about fission",
            embed=_fake_embed,
        )
        c1 = index.chunk_count()
        index.ingest(
            file_id="f1", title="T",
            content="revised content about fusion",
            embed=_fake_embed,
        )
        c2 = index.chunk_count()
        # Still only the one file's chunks, not doubled.
        assert c2 == c1
        hits = index.search("fusion", k=3)
        assert hits and any("fusion" in h.text.lower() for h in hits)
        # Original term no longer matches.
        stale = index.search("fission", k=3)
        assert not any("fission" in h.text.lower() for h in stale)


# ---------------------------------------------------------------------------
# Search — keyword path, always available
# ---------------------------------------------------------------------------


class TestSearchKeyword:
    def test_search_finds_matching_chunk(self, index):
        index.ingest(
            file_id="a", title="A",
            content="Control rods absorb neutrons to slow fission.",
            embed=_fake_embed,
        )
        index.ingest(
            file_id="b", title="B",
            content="Fuel assemblies are arranged in a lattice.",
            embed=_fake_embed,
        )
        hits = index.search("control rod", k=5)
        assert hits
        assert any("control rod" in h.text.lower() for h in hits)

    def test_search_returns_file_id_and_title_on_hits(self, index):
        index.ingest(
            file_id="abc", title="Chapter 1 — Fission",
            content="Heavy nuclei fission into lighter fragments.",
            embed=_fake_embed,
        )
        hits = index.search("fission", k=3)
        assert hits
        assert hits[0].file_id == "abc"
        assert hits[0].title == "Chapter 1 — Fission"

    def test_search_empty_index_returns_empty(self, index):
        assert index.search("anything", k=3) == []


# ---------------------------------------------------------------------------
# Graph — entity extraction + neighbor walk
# ---------------------------------------------------------------------------


class TestGraph:
    def test_ingest_records_document_entity(self, index):
        index.ingest(
            file_id="a", title="Fission notes",
            content="Some content.",
            embed=_fake_embed,
        )
        ents = index.entities()
        # Deterministic extractor always produces at least a Document node.
        assert any(e["label"] == "Document" for e in ents)

    def test_ingest_extracts_graph_edges(self, index):
        # The deterministic extractor finds cross-refs and author lines
        # matching specific patterns (NUREG/CFR/ORNL/"John Smith, Author").
        index.ingest(
            file_id="a", title="Paper",
            content=(
                "John Smith, Author\n\n"
                "This procedure refers to 10 CFR 50.2 for context."
            ),
            embed=_fake_embed,
        )
        edges = index.edges()
        assert edges
        assert all("rel_type" in e for e in edges)

    def test_graph_neighbors_of_file(self, index):
        index.ingest(
            file_id="a", title="Paper",
            content="Ana Ramirez, Author\n\nContent body.",
            embed=_fake_embed,
        )
        neighbors = index.neighbors_of_file("a")
        names = {n["name"] for n in neighbors}
        assert any("Ramirez" in n for n in names)


# ---------------------------------------------------------------------------
# Ingest without embeddings — keyword-only fallback
# ---------------------------------------------------------------------------


class TestEmbedOptional:
    def test_ingest_without_embed_still_searchable_via_fts(self, index):
        """Students on planes with no embeddings API still get keyword
        search. Vector search degrades gracefully to FTS-only."""
        index.ingest(
            file_id="a", title="A",
            content="Control rods absorb neutrons.",
            embed=None,  # no embedding — fall back to keyword
        )
        hits = index.search("control rods", k=5)
        assert hits
        assert any("control rod" in h.text.lower() for h in hits)
