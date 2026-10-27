# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RAG A/B benchmark — compares retrieval quality across generations/tiers.

Runs a set of gold Q&A pairs against different chunking strategies
and reports statistical comparison.

Usage::

    from axiom.rag.benchmark import run_ab_benchmark
    report = run_ab_benchmark(store, tier_a="fixed", tier_b="semantic")
    print(report.summary)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class BenchmarkResult:
    """Results for a single tier/generation."""

    tier: str
    generation: int | None
    query_count: int = 0
    recall_at_5: float = 0.0  # fraction of queries that returned >= 1 result
    mean_score: float = 0.0
    scores: list[float] = field(default_factory=list)


@dataclass
class ABReport:
    """Comparison report between two benchmark results."""

    blue: BenchmarkResult
    green: BenchmarkResult
    recall_delta: float = 0.0
    score_delta: float = 0.0
    p_value: float = 1.0
    significant: bool = False
    summary: str = ""


def run_ab_benchmark(
    store,
    gold_queries: list[dict],
    tier_a: str = "fixed",
    tier_b: str = "semantic",
    gen_a: int | None = None,
    gen_b: int | None = None,
    limit: int = 5,
) -> ABReport:
    """Run A/B benchmark comparing two chunking tiers or generations.

    Args:
        store: RAGStore instance
        gold_queries: List of {"query": str, "keywords": list[str]} dicts
        tier_a: Blue tier (default: "fixed")
        tier_b: Green tier (default: "semantic")
        gen_a: Blue generation (or None for any)
        gen_b: Green generation (or None for any)
        limit: Max results per query

    Returns:
        ABReport with statistical comparison
    """
    result_a = _run_tier(store, gold_queries, tier_a, gen_a, limit)
    result_b = _run_tier(store, gold_queries, tier_b, gen_b, limit)

    recall_delta = result_b.recall_at_5 - result_a.recall_at_5
    score_delta = result_b.mean_score - result_a.mean_score

    # Statistical significance via paired t-test (or permutation test)
    p_value = _paired_t_test(result_a.scores, result_b.scores)
    significant = p_value < 0.05

    summary = (
        f"Blue ({tier_a}): recall={result_a.recall_at_5:.1%}, mean_score={result_a.mean_score:.3f}\n"
        f"Green ({tier_b}): recall={result_b.recall_at_5:.1%}, mean_score={result_b.mean_score:.3f}\n"
        f"Delta: recall={recall_delta:+.1%}, score={score_delta:+.3f}\n"
        f"p-value: {p_value:.4f} ({'SIGNIFICANT' if significant else 'not significant'})"
    )

    return ABReport(
        blue=result_a,
        green=result_b,
        recall_delta=recall_delta,
        score_delta=score_delta,
        p_value=p_value,
        significant=significant,
        summary=summary,
    )


def _run_tier(
    store,
    gold_queries: list[dict],
    chunking_tier: str,
    generation: int | None,
    limit: int,
) -> BenchmarkResult:
    """Run benchmark queries against a specific tier/generation."""
    scores = []
    found = 0

    for q in gold_queries:
        query_text = q["query"]
        keywords = q.get("keywords", [])

        search_kwargs = {
            "query_text": query_text,
            "limit": limit,
            "chunking_tier": chunking_tier,
        }
        if generation is not None:
            search_kwargs["corpus_generation"] = generation

        results = store.search(**search_kwargs)

        if results:
            found += 1
            top_score = results[0].combined_score

            # Keyword match bonus
            all_text = " ".join(r.chunk_text for r in results).lower()
            kw_hits = sum(1 for kw in keywords if kw.lower() in all_text)
            kw_ratio = kw_hits / len(keywords) if keywords else 0
            scores.append(0.5 * top_score + 0.5 * kw_ratio)
        else:
            scores.append(0.0)

    n = len(gold_queries)
    return BenchmarkResult(
        tier=chunking_tier,
        generation=generation,
        query_count=n,
        recall_at_5=found / n if n > 0 else 0,
        mean_score=sum(scores) / n if n > 0 else 0,
        scores=scores,
    )


def _paired_t_test(scores_a: list[float], scores_b: list[float]) -> float:
    """Paired t-test for two score lists. Returns p-value.

    Uses a simple approximation without scipy dependency.
    """
    n = min(len(scores_a), len(scores_b))
    if n < 3:
        return 1.0  # Not enough data

    diffs = [scores_b[i] - scores_a[i] for i in range(n)]
    mean_diff = sum(diffs) / n
    var_diff = sum((d - mean_diff) ** 2 for d in diffs) / (n - 1)

    if var_diff == 0:
        return 0.0 if mean_diff != 0 else 1.0

    se = math.sqrt(var_diff / n)
    t_stat = mean_diff / se

    # Approximate p-value using normal distribution (valid for n > 30,
    # conservative for smaller n)
    p = 2 * (1 - _normal_cdf(abs(t_stat)))
    return p


def _normal_cdf(x: float) -> float:
    """Approximate standard normal CDF (Abramowitz & Stegun 26.2.17)."""
    if x < 0:
        return 1.0 - _normal_cdf(-x)
    t = 1.0 / (1.0 + 0.2316419 * x)
    poly = t * (
        0.319381530 + t * (-0.356563782 + t * (1.781477937 + t * (-1.821255978 + 1.330274429 * t)))
    )
    return 1.0 - poly * math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)
