# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Smoke tests for axiom/medallion/export.py — pseudonymize + consent filter."""

from __future__ import annotations


class TestPseudonymize:
    def test_deterministic(self):
        from axiom.medallion.export import pseudonymize

        assert pseudonymize("u1") == pseudonymize("u1")
        assert pseudonymize("u1") != pseudonymize("u2")

    def test_starts_with_anon_prefix(self):
        from axiom.medallion.export import pseudonymize

        assert pseudonymize("x").startswith("anon-")

    def test_custom_length(self):
        from axiom.medallion.export import pseudonymize

        p = pseudonymize("x", length=20)
        # "anon-" + 20 hex chars = 25 chars
        assert len(p) == 25


class TestMaybePseudonymize:
    def test_anonymize_false_passthrough(self):
        from axiom.medallion.export import maybe_pseudonymize

        assert maybe_pseudonymize("real-id", anonymize=False) == "real-id"

    def test_anonymize_true(self):
        from axiom.medallion.export import maybe_pseudonymize, pseudonymize

        assert maybe_pseudonymize("real-id", anonymize=True) == pseudonymize("real-id")

    def test_empty_string_passthrough(self):
        from axiom.medallion.export import maybe_pseudonymize

        assert maybe_pseudonymize("", anonymize=True) == ""


class TestConsentFilter:
    def test_none_allowlist_inactive(self):
        from axiom.medallion.export import consent_filter

        rows = [{"principal_id": "u1"}, {"principal_id": "u2"}]
        assert consent_filter(rows, consented_ids=None) == rows

    def test_filters_by_default_key(self):
        from axiom.medallion.export import consent_filter

        rows = [{"principal_id": "u1"}, {"principal_id": "u2"}]
        out = consent_filter(rows, consented_ids={"u1"})
        assert len(out) == 1
        assert out[0]["principal_id"] == "u1"

    def test_custom_id_key(self):
        from axiom.medallion.export import consent_filter

        rows = [{"student_id": "s1"}, {"student_id": "s2"}]
        out = consent_filter(rows, consented_ids={"s1"}, id_key="student_id")
        assert len(out) == 1
        assert out[0]["student_id"] == "s1"
