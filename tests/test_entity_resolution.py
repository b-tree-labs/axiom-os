# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for entity resolution (Stage 3)."""

from __future__ import annotations

from axiom.graph.schema import Entity


class TestEntityResolution:
    def test_exact_match_merges(self):
        from axiom.graph.resolution import resolve_entities

        entities = [Entity(label="Document", name="NUREG-0800")]
        existing = {"NUREG-0800": "Document"}

        results = resolve_entities(entities, existing)
        assert results[0].action == "merged"
        assert results[0].confidence == 1.0

    def test_new_entity_stays_new(self):
        from axiom.graph.resolution import resolve_entities

        entities = [Entity(label="Document", name="COMPLETELY-DIFFERENT-DOC")]
        existing = {"NUREG-0800": "Document"}

        results = resolve_entities(entities, existing)
        assert results[0].action == "new"

    def test_fuzzy_match_above_threshold(self):
        from axiom.graph.resolution import resolve_entities

        entities = [Entity(label="Document", name="ORNL-4396")]
        existing = {"ORNL-4397": "Document"}  # 1 char diff

        results = resolve_entities(entities, existing, threshold=0.8)
        assert results[0].action == "merged"
        assert results[0].merged_with == "ORNL-4397"

    def test_fuzzy_match_below_threshold_flagged(self):
        from axiom.graph.resolution import resolve_entities

        entities = [Entity(label="Component", name="valve-V101")]
        existing = {"valve-V-102": "Component"}  # similar but different

        results = resolve_entities(entities, existing, threshold=0.95)
        # Should be flagged (similar enough to notice, not enough to auto-merge)
        assert results[0].action in ("flagged", "new")

    def test_different_label_no_merge(self):
        from axiom.graph.resolution import resolve_entities

        entities = [Entity(label="Person", name="NUREG-0800")]
        existing = {"NUREG-0800": "Document"}  # Same name, different type

        results = resolve_entities(entities, existing)
        assert results[0].action == "new"  # Don't merge across types

    def test_case_insensitive_match(self):
        from axiom.graph.resolution import resolve_entities

        entities = [Entity(label="Material", name="UO2")]
        existing = {"uo2": "Material"}

        results = resolve_entities(entities, existing)
        assert results[0].action == "merged"


class TestLevenshtein:
    def test_identical(self):
        from axiom.graph.resolution import _levenshtein

        assert _levenshtein("hello", "hello") == 0

    def test_one_insert(self):
        from axiom.graph.resolution import _levenshtein

        assert _levenshtein("hello", "hellos") == 1

    def test_one_substitution(self):
        from axiom.graph.resolution import _levenshtein

        assert _levenshtein("hello", "hallo") == 1

    def test_empty(self):
        from axiom.graph.resolution import _levenshtein

        assert _levenshtein("", "abc") == 3
        assert _levenshtein("abc", "") == 3
