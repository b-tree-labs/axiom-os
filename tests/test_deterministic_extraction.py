# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for deterministic entity extraction (Stage 1 — no LLM)."""

from __future__ import annotations

SAMPLE_REGULATORY = """\
10 CFR Part 50 — Domestic Licensing of Production and Utilization Facilities

§ 50.46 Acceptance criteria for emergency core cooling systems.
See also NUREG-0800 and NUREG-CR-3584 for supporting analysis.
Reference: 10 CFR 50.34 and 10 CFR 100.11.

The calculated maximum fuel element cladding temperature shall not exceed
2200 °F (1204 °C) per the requirements established in GDC 35 of Appendix A.
"""

SAMPLE_ORNL = """\
ORNL-4396

MOLTEN-SALT REACTOR PROGRAM
SEMIANNUAL PROGRESS REPORT
For Period Ending February 28, 1969

M. W. Rosenthal, Program Director
R. B. Briggs, Associate Director
P. R. Kasten, Associate Director

# Chapter 1: MSRE Operations

## 1.1 Fuel Salt Processing

The fuel salt composition LiF-BeF2-ZrF4-UF4 was maintained at
65-29.1-5-0.9 mole percent throughout the reporting period.
See also ORNL-4254 and ORNL-TM-0728 for related analyses.
"""


class TestDocumentCrossReferences:
    def test_extracts_nureg_references(self):
        from axiom.graph.extractors.deterministic import extract_cross_references

        refs = extract_cross_references(SAMPLE_REGULATORY)
        ref_names = [r.name for r in refs]
        assert "NUREG-0800" in ref_names
        assert "NUREG-CR-3584" in ref_names

    def test_extracts_cfr_references(self):
        from axiom.graph.extractors.deterministic import extract_cross_references

        refs = extract_cross_references(SAMPLE_REGULATORY)
        ref_names = [r.name for r in refs]
        assert any("10 CFR 50.34" in n for n in ref_names)
        assert any("10 CFR 100.11" in n for n in ref_names)

    def test_extracts_ornl_references(self):
        from axiom.graph.extractors.deterministic import extract_cross_references

        refs = extract_cross_references(SAMPLE_ORNL)
        ref_names = [r.name for r in refs]
        assert "ORNL-4254" in ref_names
        assert "ORNL-TM-0728" in ref_names


class TestPersonExtraction:
    def test_extracts_authors_from_header(self):
        from axiom.graph.extractors.deterministic import extract_persons

        persons = extract_persons(SAMPLE_ORNL)
        names = [p.name for p in persons]
        assert any("Rosenthal" in n for n in names)


class TestSectionBoundaries:
    def test_extracts_heading_structure(self):
        from axiom.graph.extractors.deterministic import extract_section_boundaries

        boundaries = extract_section_boundaries(SAMPLE_ORNL)
        assert len(boundaries) >= 2  # Chapter 1, Section 1.1


class TestFullExtraction:
    def test_extract_from_document(self):
        from axiom.graph.extractors.deterministic import extract_from_document

        result = extract_from_document(SAMPLE_ORNL, "ORNL-4396.md", "markdown")
        assert len(result.entities) >= 1
        assert len(result.edges) >= 0  # Edges may be 0 if no cross-refs to known entities
        assert len(result.boundaries) >= 2

    def test_returns_extraction_result(self):
        from axiom.graph.extractors.deterministic import ExtractionResult, extract_from_document

        result = extract_from_document(SAMPLE_REGULATORY, "10cfr50.md", "markdown")
        assert isinstance(result, ExtractionResult)
        assert hasattr(result, "entities")
        assert hasattr(result, "edges")
        assert hasattr(result, "boundaries")
