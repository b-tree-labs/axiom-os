# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for CURIO quality gate."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from axiom.rag.store import CORPUS_COMMUNITY, SearchResult


class TestQualityGate:
    def test_importable(self):
        from axiom.extensions.builtins.research.quality_gate import evaluate_candidate
        assert callable(evaluate_candidate)

    def test_no_candidate_returns_no_promote(self):
        from axiom.extensions.builtins.research.quality_gate import evaluate_candidate

        mock_store = MagicMock()
        mock_gen = MagicMock()
        mock_gen.get_active_generation.return_value = 1
        mock_gen.get_candidate_generation.return_value = None

        result = evaluate_candidate(mock_store, mock_gen, CORPUS_COMMUNITY)
        assert result.should_promote is False
        assert "No candidate" in result.reason

    def test_promotes_on_significant_improvement(self):
        from axiom.extensions.builtins.research.quality_gate import evaluate_candidate
        from axiom.rag.quality import GenerationQuality

        mock_store = MagicMock()
        mock_store.search.side_effect = lambda **kw: (
            [SearchResult("doc.pdf", "Doc", "relevant content about reactor safety", 0, 0.9, 0.9, CORPUS_COMMUNITY)]
            if kw.get("chunking_tier") == "semantic"
            else [SearchResult("doc.pdf", "Doc", "less relevant", 0, 0.3, 0.3, CORPUS_COMMUNITY)]
        )

        mock_gen = MagicMock()
        mock_gen.get_active_generation.return_value = 1
        mock_gen.get_candidate_generation.return_value = 2

        gold = [
            {"query": f"reactor safety question {i}", "keywords": ["reactor", "safety"]}
            for i in range(20)
        ]

        with patch("axiom.extensions.builtins.research.quality_gate.compute_generation_quality",
                    return_value=GenerationQuality(CORPUS_COMMUNITY, 1, 50, 0.5, 0.5, 0.5, 40)):
            result = evaluate_candidate(mock_store, mock_gen, CORPUS_COMMUNITY, gold_queries=gold)
        assert result.should_promote is True
        assert result.p_value < 0.05

    def test_does_not_promote_on_regression(self):
        from axiom.extensions.builtins.research.quality_gate import evaluate_candidate
        from axiom.rag.quality import GenerationQuality

        mock_store = MagicMock()
        mock_store.search.side_effect = lambda **kw: (
            []
            if kw.get("chunking_tier") == "semantic"
            else [SearchResult("doc.pdf", "Doc", "good content", 0, 0.8, 0.8, CORPUS_COMMUNITY)]
        )

        mock_gen = MagicMock()
        mock_gen.get_active_generation.return_value = 1
        mock_gen.get_candidate_generation.return_value = 2

        gold = [
            {"query": f"question {i}", "keywords": ["reactor"]}
            for i in range(20)
        ]

        with patch("axiom.extensions.builtins.research.quality_gate.compute_generation_quality",
                    return_value=GenerationQuality(CORPUS_COMMUNITY, 1, 50, 0.5, 0.5, 0.5, 40)):
            result = evaluate_candidate(mock_store, mock_gen, CORPUS_COMMUNITY, gold_queries=gold)
        assert result.should_promote is False

    def test_evaluation_result_fields(self):
        from axiom.extensions.builtins.research.quality_gate import EvaluationResult

        r = EvaluationResult(
            corpus=CORPUS_COMMUNITY,
            active_generation=1,
            candidate_generation=2,
            should_promote=True,
            reason="test",
            p_value=0.01,
        )
        assert r.corpus == CORPUS_COMMUNITY
        assert r.p_value == 0.01
