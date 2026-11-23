# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for axiom/memory/auto_classifier.py.

Shape-based pre-routing to a CognitiveType. Cheap heuristic layer
before LLM-validated classification. Per MIRIX / Substrate-App
classifier.go: inspect content keys + shape to guess the manager.
"""

from __future__ import annotations


class TestProceduralDetection:
    def test_has_steps_flags_procedural(self):
        from axiom.memory.auto_classifier import classify_shape
        from axiom.memory.fragment import CognitiveType

        assert classify_shape({"workflow": "deploy", "steps": ["a", "b"]}) == CognitiveType.PROCEDURAL

    def test_has_workflow_alone_is_procedural(self):
        from axiom.memory.auto_classifier import classify_shape
        from axiom.memory.fragment import CognitiveType

        assert classify_shape({"workflow_name": "deploy", "steps": []}) == CognitiveType.PROCEDURAL


class TestResourceDetection:
    def test_has_ref_flags_resource(self):
        from axiom.memory.auto_classifier import classify_shape
        from axiom.memory.fragment import CognitiveType

        assert classify_shape({"ref": "s3://bucket/k"}) == CognitiveType.RESOURCE

    def test_has_url_flags_resource(self):
        from axiom.memory.auto_classifier import classify_shape
        from axiom.memory.fragment import CognitiveType

        assert classify_shape({"url": "https://example.com/doc.pdf"}) == CognitiveType.RESOURCE

    def test_has_file_path_flags_resource(self):
        from axiom.memory.auto_classifier import classify_shape
        from axiom.memory.fragment import CognitiveType

        assert classify_shape({"file_path": "/data/doc.pdf"}) == CognitiveType.RESOURCE


class TestEpisodicDetection:
    def test_event_time_flags_episodic(self):
        from axiom.memory.auto_classifier import classify_shape
        from axiom.memory.fragment import CognitiveType

        assert classify_shape({"event_time": "2026-04-17T10:00:00Z", "msg": "x"}) == CognitiveType.EPISODIC

    def test_timestamp_flags_episodic(self):
        from axiom.memory.auto_classifier import classify_shape
        from axiom.memory.fragment import CognitiveType

        assert classify_shape({"timestamp": "2026-04-17T10:00:00Z"}) == CognitiveType.EPISODIC


class TestSemanticDetection:
    def test_has_fact_or_concept_flags_semantic(self):
        from axiom.memory.auto_classifier import classify_shape
        from axiom.memory.fragment import CognitiveType

        assert classify_shape({"concept": "fission", "definition": "splitting"}) == CognitiveType.SEMANTIC

    def test_has_fact_flags_semantic(self):
        from axiom.memory.auto_classifier import classify_shape
        from axiom.memory.fragment import CognitiveType

        assert classify_shape({"fact": "water boils at 100C"}) == CognitiveType.SEMANTIC


class TestVaultDetection:
    def test_archived_retention_flags_vault(self):
        from axiom.memory.auto_classifier import classify_shape
        from axiom.memory.fragment import CognitiveType

        assert classify_shape({
            "summary": "old course",
            "retention_period": "P10Y",  # ISO 8601 duration
            "archived": True,
        }) == CognitiveType.VAULT


class TestCoreDetection:
    def test_essential_flag_flags_core(self):
        from axiom.memory.auto_classifier import classify_shape
        from axiom.memory.fragment import CognitiveType

        assert classify_shape({"essential": True, "key": "agent_name", "value": "axi"}) == CognitiveType.CORE


class TestAmbiguousFallback:
    def test_no_signal_returns_semantic_default(self):
        """Ambiguous content defaults to semantic (generic fact store)."""
        from axiom.memory.auto_classifier import classify_shape
        from axiom.memory.fragment import CognitiveType

        assert classify_shape({"text": "some information"}) == CognitiveType.SEMANTIC

    def test_empty_dict_raises_or_returns_none(self):
        from axiom.memory.auto_classifier import classify_shape

        assert classify_shape({}) is None


class TestPrecedence:
    """When multiple signals present, the stronger wins."""

    def test_steps_beats_timestamp(self):
        """A procedural with an event_time should still be procedural."""
        from axiom.memory.auto_classifier import classify_shape
        from axiom.memory.fragment import CognitiveType

        assert classify_shape({
            "steps": ["a", "b"], "event_time": "2026-04-17T10:00:00Z"
        }) == CognitiveType.PROCEDURAL

    def test_ref_beats_fact(self):
        """A resource with a description is still a resource."""
        from axiom.memory.auto_classifier import classify_shape
        from axiom.memory.fragment import CognitiveType

        assert classify_shape({
            "ref": "s3://x", "fact": "description of the doc"
        }) == CognitiveType.RESOURCE

    def test_vault_beats_semantic_when_archived(self):
        from axiom.memory.auto_classifier import classify_shape
        from axiom.memory.fragment import CognitiveType

        assert classify_shape({
            "fact": "old answer", "archived": True, "retention_period": "P5Y"
        }) == CognitiveType.VAULT


class TestConfidence:
    def test_strong_signal_high_confidence(self):
        from axiom.memory.auto_classifier import classify_shape_with_confidence
        from axiom.memory.fragment import CognitiveType

        ct, conf = classify_shape_with_confidence({"steps": ["a", "b", "c"]})
        assert ct == CognitiveType.PROCEDURAL
        assert conf >= 0.9

    def test_weak_signal_low_confidence(self):
        from axiom.memory.auto_classifier import classify_shape_with_confidence

        _, conf = classify_shape_with_confidence({"text": "vague content"})
        assert conf <= 0.5

    def test_empty_zero_confidence(self):
        from axiom.memory.auto_classifier import classify_shape_with_confidence

        ct, conf = classify_shape_with_confidence({})
        assert ct is None
        assert conf == 0.0
