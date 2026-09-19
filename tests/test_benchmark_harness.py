# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the A/B benchmark harness."""

from __future__ import annotations

from unittest.mock import MagicMock

from axiom.rag.store import CORPUS_COMMUNITY, SearchResult


class TestBenchmarkHarness:
    def test_importable(self):
        from axiom.rag.benchmark import run_ab_benchmark
        assert callable(run_ab_benchmark)

    def test_ab_benchmark_with_mock_store(self):
        from axiom.rag.benchmark import run_ab_benchmark

        mock_store = MagicMock()
        # Blue returns results for every query, green returns nothing
        mock_store.search.side_effect = lambda **kw: (
            [SearchResult("doc.pdf", "Doc", "reactor safety", 0, 0.8, 0.8, CORPUS_COMMUNITY)]
            if kw.get("chunking_tier") == "fixed"
            else []
        )

        gold = [
            {"query": "reactor safety", "keywords": ["reactor", "safety"]},
            {"query": "fuel composition", "keywords": ["fuel", "LiF"]},
            {"query": "temperature coefficient", "keywords": ["temperature"]},
        ]

        report = run_ab_benchmark(mock_store, gold, tier_a="fixed", tier_b="semantic")
        assert report.blue.recall_at_5 == 1.0  # fixed found all
        assert report.green.recall_at_5 == 0.0  # semantic found none
        assert report.recall_delta < 0  # green worse
        assert report.summary  # non-empty

    def test_statistical_significance_when_different(self):
        from axiom.rag.benchmark import _paired_t_test

        # Clear difference: B is consistently better
        scores_a = [0.1, 0.2, 0.15, 0.1, 0.2, 0.1, 0.15, 0.1, 0.2, 0.1]
        scores_b = [0.8, 0.9, 0.85, 0.8, 0.9, 0.8, 0.85, 0.8, 0.9, 0.8]
        p = _paired_t_test(scores_a, scores_b)
        assert p < 0.05  # Should be significant

    def test_no_significance_when_similar(self):
        from axiom.rag.benchmark import _paired_t_test

        scores_a = [0.5, 0.5, 0.5, 0.5, 0.5]
        scores_b = [0.51, 0.49, 0.5, 0.51, 0.49]
        p = _paired_t_test(scores_a, scores_b)
        assert p > 0.05  # Should NOT be significant

    def test_insufficient_data_returns_p_1(self):
        from axiom.rag.benchmark import _paired_t_test

        p = _paired_t_test([0.5], [0.6])
        assert p == 1.0  # Not enough data

    def test_normal_cdf_basic(self):
        from axiom.rag.benchmark import _normal_cdf

        assert abs(_normal_cdf(0) - 0.5) < 0.01
        assert _normal_cdf(3) > 0.99
        assert _normal_cdf(-3) < 0.01
