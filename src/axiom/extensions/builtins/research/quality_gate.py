# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CURIO quality gate — evaluates candidate generations for promotion.

Runs A/B benchmarks between active (blue) and candidate (green) generations,
using statistical significance (p < 0.05) to decide promotion.

Each corpus tier is evaluated independently. No global promotion.

Usage::

    from axiom.extensions.builtins.research.quality_gate import evaluate_candidate

    result = evaluate_candidate(store, gen_manager, "rag-community")
    if result.should_promote:
        gen_manager.promote("rag-community", result.candidate_generation)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from axiom.rag.benchmark import run_ab_benchmark
from axiom.rag.generation import GenerationManager
from axiom.rag.quality import compute_generation_quality

log = logging.getLogger(__name__)


@dataclass
class EvaluationResult:
    """Result of a CURIO quality gate evaluation."""

    corpus: str
    active_generation: int
    candidate_generation: int | None
    should_promote: bool = False
    reason: str = ""
    p_value: float = 1.0
    recall_delta: float = 0.0
    score_delta: float = 0.0


def evaluate_candidate(
    store,
    gen_manager: GenerationManager,
    corpus: str,
    gold_queries: list[dict] | None = None,
    min_queries: int = 100,
) -> EvaluationResult:
    """Evaluate whether a candidate generation should be promoted.

    Args:
        store: RAGStore instance
        gen_manager: GenerationManager instance
        corpus: Corpus tier to evaluate
        gold_queries: Benchmark queries. If None, uses retrieval_log data.
        min_queries: Minimum queries before evaluation is valid

    Returns:
        EvaluationResult with promotion decision
    """
    active = gen_manager.get_active_generation(corpus)
    candidate = gen_manager.get_candidate_generation(corpus)

    if candidate is None:
        return EvaluationResult(
            corpus=corpus,
            active_generation=active,
            candidate_generation=None,
            should_promote=False,
            reason="No candidate generation to evaluate",
        )

    # Check if we have enough data
    active_quality = compute_generation_quality(store, corpus, active)
    candidate_quality = compute_generation_quality(store, corpus, candidate)

    total_queries = active_quality.query_count + candidate_quality.query_count
    if total_queries < min_queries and gold_queries is None:
        return EvaluationResult(
            corpus=corpus,
            active_generation=active,
            candidate_generation=candidate,
            should_promote=False,
            reason=f"Insufficient data: {total_queries}/{min_queries} queries",
        )

    # Run A/B benchmark if gold queries provided
    if gold_queries:
        # Determine chunking tiers for each generation (metadata lookup)
        report = run_ab_benchmark(
            store,
            gold_queries,
            tier_a="fixed",
            tier_b="semantic",
            gen_a=active,
            gen_b=candidate,
        )

        return EvaluationResult(
            corpus=corpus,
            active_generation=active,
            candidate_generation=candidate,
            should_promote=report.significant
            and (report.recall_delta > 0 or report.score_delta > 0),
            reason=report.summary,
            p_value=report.p_value,
            recall_delta=report.recall_delta,
            score_delta=report.score_delta,
        )

    # Use retrieval_log quality metrics
    if candidate_quality.mean_similarity > active_quality.mean_similarity:
        # Simple heuristic when no gold queries available
        improvement = candidate_quality.mean_similarity - active_quality.mean_similarity
        return EvaluationResult(
            corpus=corpus,
            active_generation=active,
            candidate_generation=candidate,
            should_promote=improvement > 0.05,  # 5% improvement threshold
            reason=(
                f"Candidate similarity: {candidate_quality.mean_similarity:.3f} vs "
                f"active: {active_quality.mean_similarity:.3f} "
                f"(delta: {improvement:+.3f})"
            ),
            score_delta=improvement,
        )

    return EvaluationResult(
        corpus=corpus,
        active_generation=active,
        candidate_generation=candidate,
        should_promote=False,
        reason=(
            f"Candidate not better: similarity {candidate_quality.mean_similarity:.3f} vs "
            f"active {active_quality.mean_similarity:.3f}"
        ),
    )
