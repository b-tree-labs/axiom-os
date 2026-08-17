# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RAG corpus generation lifecycle — blue/green upgrades.

Each corpus tier (community/facility/personal) manages generations
independently. A generation is a complete rebuild of the corpus
(new source data, new chunking strategy, new embeddings).

Blue = active_generation (serving queries)
Green = candidate_generation (under evaluation)

Lifecycle:
  create_candidate() → build chunks → CURIO A/B evaluates →
  promote() or discard() → next candidate can start
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Schema for generation config — created in RAGStore.connect() migration
_GENERATION_CONFIG_DDL = """\
CREATE TABLE IF NOT EXISTS rag_generation_config (
    corpus          TEXT PRIMARY KEY,
    active_generation INTEGER NOT NULL DEFAULT 1,
    candidate_generation INTEGER,
    min_queries_for_eval INTEGER NOT NULL DEFAULT 100,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_RETRIEVAL_LOG_DDL = """\
CREATE TABLE IF NOT EXISTS retrieval_log (
    id              BIGSERIAL PRIMARY KEY,
    query_hash      TEXT NOT NULL,
    corpus          TEXT NOT NULL,
    generation      INTEGER NOT NULL,
    chunking_tier   TEXT,
    result_count    INTEGER,
    top_similarity  FLOAT,
    user_feedback   SMALLINT,
    latency_ms      INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_retrieval_log_corpus_gen
    ON retrieval_log (corpus, generation);
"""


class GenerationManager:
    """Manages blue/green RAG generations per corpus tier."""

    def __init__(self, store) -> None:
        self._store = store
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        """Create generation config tables if they don't exist."""
        try:
            conn = self._store._conn
            if conn is None:
                return
            with conn.cursor() as cur:
                cur.execute(_GENERATION_CONFIG_DDL)
                cur.execute(_RETRIEVAL_LOG_DDL)
        except Exception:
            pass  # Tables may already exist or DB not available

    def get_active_generation(self, corpus: str) -> int:
        """Get the active (blue) generation for a corpus. Default 1."""
        conn = self._store._conn
        if conn is None:
            return 1
        with conn.cursor() as cur:
            cur.execute(
                "SELECT active_generation FROM rag_generation_config WHERE corpus = %s",
                (corpus,),
            )
            row = cur.fetchone()
            if row is None:
                # Initialize default
                cur.execute(
                    "INSERT INTO rag_generation_config (corpus, active_generation) "
                    "VALUES (%s, 1) ON CONFLICT (corpus) DO NOTHING",
                    (corpus,),
                )
                return 1
            return row[0]

    def get_candidate_generation(self, corpus: str) -> int | None:
        """Get the candidate (green) generation, or None if no candidate."""
        conn = self._store._conn
        if conn is None:
            return None
        with conn.cursor() as cur:
            cur.execute(
                "SELECT candidate_generation FROM rag_generation_config WHERE corpus = %s",
                (corpus,),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def create_candidate(self, corpus: str) -> int:
        """Create a new candidate generation (next integer after active)."""
        active = self.get_active_generation(corpus)
        candidate = active + 1
        conn = self._store._conn
        assert conn is not None
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE rag_generation_config SET candidate_generation = %s, "
                "updated_at = now() WHERE corpus = %s",
                (candidate, corpus),
            )
        log.info("Created candidate generation %d for %s (active: %d)", candidate, corpus, active)
        return candidate

    def promote(self, corpus: str, generation: int) -> None:
        """Promote a generation to active (blue). Clears candidate."""
        conn = self._store._conn
        assert conn is not None
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE rag_generation_config SET active_generation = %s, "
                "candidate_generation = NULL, updated_at = now() WHERE corpus = %s",
                (generation, corpus),
            )
        log.info("Promoted generation %d to active for %s", generation, corpus)

    def discard(self, corpus: str, generation: int) -> None:
        """Discard a candidate generation without changing active."""
        conn = self._store._conn
        assert conn is not None
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE rag_generation_config SET candidate_generation = NULL, "
                "updated_at = now() WHERE corpus = %s AND candidate_generation = %s",
                (corpus, generation),
            )
        log.info("Discarded candidate generation %d for %s", generation, corpus)

    def rollback(self, corpus: str, target_generation: int) -> None:
        """Rollback active generation to a previous one."""
        conn = self._store._conn
        assert conn is not None
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE rag_generation_config SET active_generation = %s, "
                "candidate_generation = NULL, updated_at = now() WHERE corpus = %s",
                (target_generation, corpus),
            )
        log.info("Rolled back %s to generation %d", corpus, target_generation)

    def get_config(self, corpus: str) -> dict:
        """Get full generation config for a corpus."""
        conn = self._store._conn
        if conn is None:
            return {"corpus": corpus, "active_generation": 1, "candidate_generation": None}
        with conn.cursor() as cur:
            cur.execute(
                "SELECT corpus, active_generation, candidate_generation, "
                "min_queries_for_eval, updated_at FROM rag_generation_config WHERE corpus = %s",
                (corpus,),
            )
            row = cur.fetchone()
            if row is None:
                return {"corpus": corpus, "active_generation": 1, "candidate_generation": None}
            return {
                "corpus": row[0],
                "active_generation": row[1],
                "candidate_generation": row[2],
                "min_queries_for_eval": row[3],
                "updated_at": str(row[4]) if row[4] else None,
            }
