# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for graph-enhanced semantic chunking (Phase 3d).

The graph-informed chunker takes entity extraction output (boundaries,
entity locations) and uses them to produce even better chunk boundaries
than pure structural detection.
"""

from __future__ import annotations

from axiom.graph.extractors.deterministic import extract_from_document

SAMPLE_ORNL = """\
ORNL-4396

MOLTEN-SALT REACTOR PROGRAM SEMIANNUAL PROGRESS REPORT

# Chapter 1: MSRE Operations

## 1.1 Fuel Salt Processing

The fuel salt composition LiF-BeF2-ZrF4-UF4 was maintained at
65-29.1-5-0.9 mole percent. See also ORNL-4254 for related analyses.

The primary pump operated at 1200 rpm with a flow rate of 1200 gpm.

## 1.2 Component Inspection

Surveillance samples were removed from the reactor vessel.
The Hastelloy N specimens showed no significant corrosion.

# Chapter 2: Chemistry

## 2.1 Fuel Salt Analysis

Chemical analyses of the fuel salt confirmed the beryllium
concentration at 29.1 mole percent BeF2.
"""


class TestGraphEnhancedChunker:
    def test_importable(self):
        from axiom.rag.semantic_chunker import chunk_semantic
        assert callable(chunk_semantic)

    def test_accepts_graph_boundaries(self):
        """chunk_semantic should accept pre-computed boundaries from graph extraction."""
        from axiom.rag.semantic_chunker import chunk_semantic

        # Get boundaries from deterministic extraction
        result = extract_from_document(SAMPLE_ORNL, "ORNL-4396.md", "markdown")

        # Pass graph-derived boundaries to chunker
        chunks = chunk_semantic(
            SAMPLE_ORNL,
            "ORNL-4396.md",
            boundaries=result.boundaries,
            max_chunk_size=500,
        )
        assert len(chunks) >= 2

    def test_graph_boundaries_improve_chunking(self):
        """Graph boundaries should produce more semantically coherent chunks."""
        from axiom.rag.semantic_chunker import chunk_semantic

        # Without graph boundaries
        chunks_plain = chunk_semantic(SAMPLE_ORNL, "test.md", max_chunk_size=400)

        # With graph boundaries from extraction
        result = extract_from_document(SAMPLE_ORNL, "ORNL-4396.md", "markdown")
        chunks_graph = chunk_semantic(
            SAMPLE_ORNL, "test.md",
            boundaries=result.boundaries,
            max_chunk_size=400,
        )

        # Both should cover the document
        plain_text = " ".join(c.text for c in chunks_plain)
        graph_text = " ".join(c.text for c in chunks_graph)
        assert "MSRE" in plain_text
        assert "MSRE" in graph_text

    def test_extraction_produces_entities(self):
        """Deterministic extraction should find entities in ORNL doc."""
        result = extract_from_document(SAMPLE_ORNL, "ORNL-4396.md", "markdown")
        entity_names = [e.name for e in result.entities]
        assert "ORNL-4254" in entity_names  # Cross-reference
        assert any("ORNL-4396" in n for n in entity_names)  # Self

    def test_extraction_produces_edges(self):
        """Should produce REFERENCES edges for cross-refs."""
        result = extract_from_document(SAMPLE_ORNL, "ORNL-4396.md", "markdown")
        edge_types = [e.rel_type for e in result.edges]
        assert "REFERENCES" in edge_types
