# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RAG retrieval quality measurement for CURIO A/B evaluation.

Logs every retrieval event with generation provenance.
Computes per-generation quality metrics for statistical comparison.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class GenerationQuality:
    """Quality metrics for a single corpus generation."""

    corpus: str
    generation: int
    query_count: int
    mean_similarity: float
    p50_similarity: float
    feedback_ratio: float  # positive_feedback / total_with_feedback
    mean_latency_ms: float


def log_retrieval(
    store,
    query_hash: str,
    corpus: str,
    generation: int,
    chunking_tier: str = "",
    result_count: int = 0,
    top_similarity: float = 0.0,
    user_feedback: int | None = None,
    latency_ms: int = 0,
) -> None:
    """Log a retrieval event for A/B quality measurement."""
    conn = store._conn
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO retrieval_log "
                "(query_hash, corpus, generation, chunking_tier, "
                " result_count, top_similarity, user_feedback, latency_ms) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    query_hash,
                    corpus,
                    generation,
                    chunking_tier,
                    result_count,
                    top_similarity,
                    user_feedback,
                    latency_ms,
                ),
            )
    except Exception as e:
        log.warning("Could not log retrieval: %s", e)


def compute_generation_quality(
    store,
    corpus: str,
    generation: int,
    window_days: int = 30,
) -> GenerationQuality:
    """Compute aggregate quality metrics for a generation."""
    conn = store._conn
    if conn is None:
        return GenerationQuality(
            corpus=corpus,
            generation=generation,
            query_count=0,
            mean_similarity=0.0,
            p50_similarity=0.0,
            feedback_ratio=0.0,
            mean_latency_ms=0.0,
        )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), "
            "  coalesce(avg(top_similarity), 0), "
            "  coalesce(percentile_cont(0.5) WITHIN GROUP (ORDER BY top_similarity), 0), "
            "  coalesce(avg(latency_ms), 0) "
            "FROM retrieval_log "
            "WHERE corpus = %s AND generation = %s "
            "  AND created_at > now() - interval '%s days'",
            (corpus, generation, window_days),
        )
        row = cur.fetchone()
        query_count = row[0] or 0
        mean_sim = float(row[1])
        p50_sim = float(row[2])
        mean_lat = float(row[3])

        # Feedback ratio
        cur.execute(
            "SELECT "
            "  count(*) FILTER (WHERE user_feedback = 1), "
            "  count(*) FILTER (WHERE user_feedback IS NOT NULL) "
            "FROM retrieval_log "
            "WHERE corpus = %s AND generation = %s "
            "  AND created_at > now() - interval '%s days'",
            (corpus, generation, window_days),
        )
        fb_row = cur.fetchone()
        positive = fb_row[0] or 0
        total_fb = fb_row[1] or 0
        feedback_ratio = positive / total_fb if total_fb > 0 else 0.0

    return GenerationQuality(
        corpus=corpus,
        generation=generation,
        query_count=query_count,
        mean_similarity=mean_sim,
        p50_similarity=p50_sim,
        feedback_ratio=feedback_ratio,
        mean_latency_ms=mean_lat,
    )


def query_hash(query_text: str) -> str:
    """Compute a stable hash for a query string."""
    return hashlib.sha256(query_text.strip().lower().encode()).hexdigest()[:16]
