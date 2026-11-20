# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the semantic chunker.

TDD: the semantic chunker splits documents at structural boundaries
(headings, tables, code blocks, page breaks) instead of fixed character counts.
"""

from __future__ import annotations

from axiom.rag.chunker import Chunk

SAMPLE_MARKDOWN = """\
# Chapter 1: Introduction

This is the introductory paragraph about molten salt reactors.
The MSRE was operated at Oak Ridge National Laboratory.

## 1.1 Background

The Molten-Salt Reactor Experiment (MSRE) was a 10 MW thermal reactor.
It used a fuel salt of LiF-BeF2-ZrF4-UF4 and graphite moderator.
Operations began in 1965 and continued until 1969.

## 1.2 Fuel Salt Composition

| Component | Mole % |
|-----------|--------|
| LiF       | 65.0   |
| BeF2      | 29.1   |
| ZrF4      | 5.0    |
| UF4       | 0.9    |

The eutectic composition was chosen for its low melting point.

# Chapter 2: Reactor Physics

## 2.1 Neutron Transport

The neutron transport equation was solved using multi-group methods.
Cross sections were generated from ENDF/B data.

```python
# Example: OpenMC model
model = openmc.Model()
fuel = openmc.Material()
fuel.add_nuclide('U235', 0.009)
```

## 2.2 Temperature Coefficients

The temperature coefficient of reactivity was strongly negative.
This is the primary safety feature of molten salt reactors.
"""

SAMPLE_REGULATORY = """\
10 CFR Part 50 — Domestic Licensing of Production and Utilization Facilities

§ 50.46 Acceptance criteria for emergency core cooling systems.

(a) Each boiling or pressurized light-water nuclear power reactor must be provided
with an emergency core cooling system (ECCS) that must be designed so that its
calculated cooling performance following postulated loss-of-coolant accidents
conforms to the criteria set forth in paragraph (b) of this section.

(b) ECCS acceptance criteria:

(1) Peak cladding temperature. The calculated maximum fuel element cladding
temperature shall not exceed 2200 °F (1204 °C).

(2) Maximum cladding oxidation. The calculated total oxidation of the cladding
shall nowhere exceed 0.17 times the total cladding thickness before oxidation.

(3) Maximum hydrogen generation. The calculated total amount of hydrogen generated
shall not exceed 0.01 times the hypothetical amount that would be generated if
all of the metal cladding surrounding the fuel were to react.

§ 50.47 Emergency plans.

(a) No operating license will be issued unless a finding is made that there is
reasonable assurance that adequate protective measures can and will be taken
in the event of a radiological emergency.
"""


class TestSemanticBoundaryDetection:
    def test_detects_heading_boundaries(self):
        from axiom.rag.semantic_chunker import detect_boundaries
        boundaries = detect_boundaries(SAMPLE_MARKDOWN)
        heading_bounds = [b for b in boundaries if b.boundary_type == "heading"]
        assert len(heading_bounds) >= 4  # Ch1, 1.1, 1.2, Ch2, 2.1, 2.2

    def test_detects_table_boundaries(self):
        from axiom.rag.semantic_chunker import detect_boundaries
        boundaries = detect_boundaries(SAMPLE_MARKDOWN)
        table_bounds = [b for b in boundaries if b.boundary_type == "table"]
        assert len(table_bounds) >= 1

    def test_detects_code_block_boundaries(self):
        from axiom.rag.semantic_chunker import detect_boundaries
        boundaries = detect_boundaries(SAMPLE_MARKDOWN)
        code_bounds = [b for b in boundaries if b.boundary_type == "code_block"]
        assert len(code_bounds) >= 1

    def test_detects_regulatory_section_boundaries(self):
        from axiom.rag.semantic_chunker import detect_boundaries
        boundaries = detect_boundaries(SAMPLE_REGULATORY)
        section_bounds = [b for b in boundaries if b.boundary_type in ("heading", "section")]
        assert len(section_bounds) >= 2  # §50.46 and §50.47


class TestSemanticChunking:
    def test_chunks_at_section_boundaries(self):
        from axiom.rag.semantic_chunker import chunk_semantic
        chunks = chunk_semantic(SAMPLE_MARKDOWN, "test.md", max_chunk_size=500)
        assert len(chunks) >= 3  # At least 3 sections with 500 char max
        assert all(isinstance(c, Chunk) for c in chunks)

    def test_table_stays_intact(self):
        """Tables should not be split across chunks."""
        from axiom.rag.semantic_chunker import chunk_semantic
        chunks = chunk_semantic(SAMPLE_MARKDOWN, "test.md")
        table_chunks = [c for c in chunks if "|" in c.text and "---" in c.text]
        assert len(table_chunks) >= 1
        # The full table should be in one chunk
        for tc in table_chunks:
            assert "LiF" in tc.text and "UF4" in tc.text

    def test_code_block_stays_intact(self):
        """Code blocks should not be split."""
        from axiom.rag.semantic_chunker import chunk_semantic
        chunks = chunk_semantic(SAMPLE_MARKDOWN, "test.md")
        code_chunks = [c for c in chunks if "openmc" in c.text.lower()]
        assert len(code_chunks) >= 1

    def test_respects_max_chunk_size(self):
        from axiom.rag.semantic_chunker import chunk_semantic
        chunks = chunk_semantic(SAMPLE_MARKDOWN, "test.md", max_chunk_size=2000)
        for c in chunks:
            assert len(c.text) <= 2000

    def test_covers_full_document(self):
        """All text from the source must appear in at least one chunk."""
        from axiom.rag.semantic_chunker import chunk_semantic
        chunks = chunk_semantic(SAMPLE_MARKDOWN, "test.md")
        all_text = " ".join(c.text for c in chunks)
        # Key phrases from every section
        assert "MSRE" in all_text
        assert "LiF" in all_text
        assert "neutron transport" in all_text.lower()
        assert "openmc" in all_text.lower()

    def test_returns_chunk_dataclass(self):
        from axiom.rag.semantic_chunker import chunk_semantic
        chunks = chunk_semantic(SAMPLE_MARKDOWN, "test.md")
        for c in chunks:
            assert hasattr(c, "text")
            assert hasattr(c, "source_path")
            assert hasattr(c, "chunk_index")
            assert c.source_path == "test.md"

    def test_fallback_to_fixed_on_empty(self):
        """Empty or very short text falls back gracefully."""
        from axiom.rag.semantic_chunker import chunk_semantic
        chunks = chunk_semantic("Short text.", "test.md")
        assert len(chunks) >= 1

    def test_regulatory_sections_split_at_sections(self):
        from axiom.rag.semantic_chunker import chunk_semantic
        chunks = chunk_semantic(SAMPLE_REGULATORY, "10cfr50.md")
        # §50.46 and §50.47 should be separate chunks (or at least separate sections)
        texts = [c.text for c in chunks]
        has_46 = any("50.46" in t for t in texts)
        has_47 = any("50.47" in t for t in texts)
        assert has_46 and has_47
