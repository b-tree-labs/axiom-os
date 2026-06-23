# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for export control screening."""

from axiom.rag.ec_screening import screen_batch, screen_document


class TestECScreening:
    def test_clean_doc_allowed(self):
        r = screen_document("ORNL-4396.pdf", "MSRE operations experience report")
        assert r.allowed_community is True

    def test_ss_filename_flagged(self):
        r = screen_document("20071116 SS ML073180552.pdf", "Some content")
        assert r.allowed_community is False
        # Filename markings are sensitive-level → human review (the rewrite no
        # longer auto-routes to org; provenance routing owns tier decisions).
        assert r.recommendation == "review"

    def test_ouo_content_flagged(self):
        r = screen_document("doc.pdf", "This document contains Official Use Only - Security Related Information")
        assert r.allowed_community is False

    def test_sunsi_flagged(self):
        r = screen_document("doc.pdf", "SUNSI Review Complete - document cleared")
        assert r.allowed_community is False

    def test_scientific_classified_not_flagged(self):
        """'classified as Class A experiment' is scientific, not security."""
        r = screen_document("doc.pdf", "experiments were classified as either Class A or Class B")
        assert r.allowed_community is True

    def test_regulatory_text_about_classification_not_flagged(self):
        """10 CFR discussing classification policy should pass."""
        r = screen_document("10CFR-Part50.pdf", "The Commission may classify information as restricted")
        assert r.allowed_community is True

    def test_unmarked_doc_allowed_on_any_tier(self):
        """Screening now runs on every tier (not community-only); an unmarked
        document passes regardless of the target corpus."""
        r = screen_document("notes.pdf", "ordinary reactor physics notes", target_corpus="rag-org")
        assert r.allowed_community is True
        assert r.severity == "none"

    def test_batch_screening(self):
        docs = [
            ("clean.pdf", "Normal reactor physics document"),
            ("SS flagged.pdf", "Some content"),
            ("also-clean.md", "More normal content"),
        ]
        allowed, flagged = screen_batch(docs)
        assert len(allowed) == 2
        assert len(flagged) == 1
        assert flagged[0].path == "SS flagged.pdf"
