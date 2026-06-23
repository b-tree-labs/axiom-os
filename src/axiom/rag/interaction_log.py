# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Interaction logging for CURIO quality measurement.

Logs every RAG query + results + user feedback for A/B evaluation
between corpus generations. Each log entry records which generation
and chunking tier served the query.

Usage::

    from axiom.rag.interaction_log import log_interaction, get_interactions

    log_interaction(store, query="reactor safety", generation=2, ...)
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

_INTERACTION_LOG_DDL = """\
CREATE TABLE IF NOT EXISTS interaction_log (
    id              BIGSERIAL PRIMARY KEY,
    session_id      TEXT,
    query_text      TEXT NOT NULL,
    query_hash      TEXT NOT NULL,
    corpus          TEXT NOT NULL,
    generation      INTEGER NOT NULL DEFAULT 1,
    chunking_tier   TEXT,
    chunks_retrieved INTEGER DEFAULT 0,
    top_similarity  FLOAT DEFAULT 0.0,
    retrieval_latency_ms INTEGER DEFAULT 0,
    response_text   TEXT,
    user_feedback   SMALLINT,
    correction_text TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_interaction_log_corpus_gen
    ON interaction_log (corpus, generation);
CREATE INDEX IF NOT EXISTS idx_interaction_log_query_hash
    ON interaction_log (query_hash);
"""


@dataclass
class InteractionEntry:
    """A single logged interaction."""

    query_text: str
    query_hash: str
    corpus: str
    generation: int
    chunking_tier: str = ""
    chunks_retrieved: int = 0
    top_similarity: float = 0.0
    retrieval_latency_ms: int = 0
    session_id: str = ""
    user_feedback: int | None = None


def ensure_interaction_log(conn) -> None:
    """Create the interaction_log table if it doesn't exist."""
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(_INTERACTION_LOG_DDL)
    except Exception:
        pass


def log_interaction(
    store,
    query_text: str,
    corpus: str,
    generation: int = 1,
    chunking_tier: str = "",
    chunks_retrieved: int = 0,
    top_similarity: float = 0.0,
    retrieval_latency_ms: int = 0,
    session_id: str = "",
    response_text: str = "",
    user_feedback: int | None = None,
    is_benchmark: bool = False,
) -> None:
    """Log a RAG interaction for quality measurement.

    Set is_benchmark=True for evaluation queries so CURIO excludes them
    from the learning loop (prevents overfitting to benchmark questions).
    """
    if is_benchmark:
        return  # Never log benchmark queries — they would poison CURIO's learning
    conn = store._conn
    if conn is None:
        return

    qhash = hashlib.sha256(query_text.strip().lower().encode()).hexdigest()[:16]

    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO interaction_log "
                "(session_id, query_text, query_hash, corpus, generation, "
                " chunking_tier, chunks_retrieved, top_similarity, "
                " retrieval_latency_ms, response_text, user_feedback) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    session_id,
                    query_text,
                    qhash,
                    corpus,
                    generation,
                    chunking_tier,
                    chunks_retrieved,
                    top_similarity,
                    retrieval_latency_ms,
                    response_text,
                    user_feedback,
                ),
            )
    except Exception as e:
        log.warning("Could not log interaction: %s", e)


def record_feedback(
    store,
    query_hash: str,
    feedback: int,
    correction_text: str = "",
) -> None:
    """Record user feedback on the most recent interaction for a query."""
    conn = store._conn
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE interaction_log SET user_feedback = %s, correction_text = %s "
                "WHERE query_hash = %s AND user_feedback IS NULL "
                "ORDER BY created_at DESC LIMIT 1",
                (feedback, correction_text, query_hash),
            )
    except Exception as e:
        log.warning("Could not record feedback: %s", e)


def prune_interactions(
    store,
    retention_days: int = 90,
    keep_with_feedback: bool = True,
) -> int:
    """Remove old interaction log entries, keeping recent and feedback-tagged.

    Args:
        store: RAGStore instance
        retention_days: Delete entries older than this (default 90 days)
        keep_with_feedback: Never delete entries that have user feedback

    Returns:
        Number of entries deleted
    """
    conn = store._conn
    if conn is None:
        return 0

    try:
        with conn.cursor() as cur:
            if keep_with_feedback:
                cur.execute(
                    "DELETE FROM interaction_log "
                    "WHERE created_at < now() - interval '%s days' "
                    "AND user_feedback IS NULL",
                    (retention_days,),
                )
            else:
                cur.execute(
                    "DELETE FROM interaction_log WHERE created_at < now() - interval '%s days'",
                    (retention_days,),
                )
            deleted = cur.rowcount
            log.info(
                "Pruned %d interaction log entries (retention=%d days)", deleted, retention_days
            )
            return deleted
    except Exception as e:
        log.warning("Could not prune interactions: %s", e)
        return 0


def get_interactions(
    store,
    corpus: str,
    generation: int | None = None,
    limit: int = 100,
) -> list[InteractionEntry]:
    """Retrieve logged interactions for analysis."""
    conn = store._conn
    if conn is None:
        return []

    try:
        with conn.cursor() as cur:
            if generation is not None:
                cur.execute(
                    "SELECT query_text, query_hash, corpus, generation, chunking_tier, "
                    "chunks_retrieved, top_similarity, retrieval_latency_ms, session_id, "
                    "user_feedback FROM interaction_log "
                    "WHERE corpus = %s AND generation = %s "
                    "ORDER BY created_at DESC LIMIT %s",
                    (corpus, generation, limit),
                )
            else:
                cur.execute(
                    "SELECT query_text, query_hash, corpus, generation, chunking_tier, "
                    "chunks_retrieved, top_similarity, retrieval_latency_ms, session_id, "
                    "user_feedback FROM interaction_log "
                    "WHERE corpus = %s ORDER BY created_at DESC LIMIT %s",
                    (corpus, limit),
                )

            return [
                InteractionEntry(
                    query_text=r[0],
                    query_hash=r[1],
                    corpus=r[2],
                    generation=r[3],
                    chunking_tier=r[4] or "",
                    chunks_retrieved=r[5] or 0,
                    top_similarity=float(r[6] or 0),
                    retrieval_latency_ms=r[7] or 0,
                    session_id=r[8] or "",
                    user_feedback=r[9],
                )
                for r in cur.fetchall()
            ]
    except Exception as e:
        log.warning("Could not read interactions: %s", e)
        return []
