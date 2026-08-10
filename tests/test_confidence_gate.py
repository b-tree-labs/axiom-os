# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for confidence-gated RAG context injection.

TDD: CURIO Research Task 1 — learn when RAG context helps vs hurts.
"""

from __future__ import annotations


class TestConfidenceGate:
    def test_importable(self):
        from axiom.extensions.builtins.research.confidence_gate import ConfidenceGate
        assert ConfidenceGate is not None

    def test_default_threshold(self):
        from axiom.extensions.builtins.research.confidence_gate import ConfidenceGate
        gate = ConfidenceGate()
        assert gate.threshold > 0.0
        assert gate.threshold < 1.0

    def test_should_inject_above_threshold(self):
        from axiom.extensions.builtins.research.confidence_gate import ConfidenceGate
        gate = ConfidenceGate(threshold=0.5)
        assert gate.should_inject(top_similarity=0.8) is True

    def test_should_not_inject_below_threshold(self):
        from axiom.extensions.builtins.research.confidence_gate import ConfidenceGate
        gate = ConfidenceGate(threshold=0.5)
        assert gate.should_inject(top_similarity=0.3) is False

    def test_should_inject_no_results(self):
        from axiom.extensions.builtins.research.confidence_gate import ConfidenceGate
        gate = ConfidenceGate(threshold=0.5)
        assert gate.should_inject(top_similarity=0.0) is False

    def test_record_outcome(self):
        from axiom.extensions.builtins.research.confidence_gate import ConfidenceGate
        gate = ConfidenceGate()
        gate.record_outcome(similarity=0.8, rag_helped=True)
        gate.record_outcome(similarity=0.3, rag_helped=False)
        assert len(gate.outcomes) == 2

    def test_recalibrate_adjusts_threshold(self):
        from axiom.extensions.builtins.research.confidence_gate import ConfidenceGate
        gate = ConfidenceGate(threshold=0.5)
        # RAG helps above 0.7, hurts below
        for _ in range(20):
            gate.record_outcome(similarity=0.8, rag_helped=True)
            gate.record_outcome(similarity=0.3, rag_helped=False)
        gate.recalibrate()
        # Threshold should have moved toward the boundary
        assert gate.threshold != 0.5

    def test_recalibrate_needs_min_samples(self):
        from axiom.extensions.builtins.research.confidence_gate import ConfidenceGate
        gate = ConfidenceGate(threshold=0.5)
        gate.record_outcome(similarity=0.8, rag_helped=True)
        old = gate.threshold
        gate.recalibrate()
        assert gate.threshold == old  # Not enough data to change


class TestABComparison:
    def test_importable(self):
        from axiom.extensions.builtins.research.confidence_gate import run_ab_for_query
        assert callable(run_ab_for_query)
