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

Tables (Postgres only; bootstrapped with idempotent DDL, no Alembic):

  rag_generation_config  per-corpus active/candidate generation pointers
  rag_generation_log     per-query generation-quality samples (query hash,
                         generation, top similarity, feedback, latency) that
                         ``axiom.rag.quality`` writes and CURIO A/B reads

``rag_generation_log`` was named ``retrieval_log`` until P2b. That name belongs
to the per-request retrieval *audit* log (principal, query text, mode, k, the
returned results): site nodes create a ``retrieval_log`` of that shape in the
same database, and with two ``CREATE TABLE IF NOT EXISTS`` competing for one
unqualified name, whichever ran second silently kept the other's table and
every one of its INSERTs failed from then on. :func:`ensure_generation_schema`
renames a legacy generation-quality ``retrieval_log`` in place before creating
anything; an audit-shaped ``retrieval_log`` is never touched.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

GENERATION_LOG_TABLE = "rag_generation_log"
LEGACY_GENERATION_LOG_TABLE = "retrieval_log"
_GENERATION_LOG_INDEX = "idx_rag_generation_log_corpus_gen"
_LEGACY_GENERATION_LOG_INDEX = "idx_retrieval_log_corpus_gen"

# A legacy ``retrieval_log`` is ours (generation-quality) only when it has
# BOTH of these columns and NONE of the audit-log markers below.
_GENERATION_LOG_MARKERS = frozenset({"query_hash", "generation"})
# Columns only the per-request retrieval audit log has (harness spec shape:
# principal, source, query_text, mode, k, result_count, results, latency_ms, note).
_AUDIT_LOG_MARKERS = frozenset({"query_text", "principal"})

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

# Generation-quality log — one row per sampled retrieval, keyed by generation
# so CURIO can compare blue vs green. Written by axiom.rag.quality.log_retrieval.
_RAG_GENERATION_LOG_DDL = """\
CREATE TABLE IF NOT EXISTS rag_generation_log (
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

CREATE INDEX IF NOT EXISTS idx_rag_generation_log_corpus_gen
    ON rag_generation_log (corpus, generation);
"""

_COLUMNS_SQL = (
    "SELECT column_name FROM information_schema.columns "
    "WHERE table_schema = current_schema() AND table_name = %s"
)


def _table_columns(cur, table: str) -> frozenset[str]:
    """Column names of ``table`` in the current schema; empty when it does not exist."""
    cur.execute(_COLUMNS_SQL, (table,))
    return frozenset(row[0] for row in cur.fetchall())


def migrate_legacy_generation_log(cur) -> bool:
    """Rename a legacy generation-quality ``retrieval_log`` to ``rag_generation_log``.

    Idempotent and additive: nothing is ever dropped. Returns True when a
    rename happened. The rename fires only when ``retrieval_log`` exists in
    the current schema with the generation-quality shape (``query_hash`` and
    ``generation`` present) and none of the audit-log columns (``query_text``
    / ``principal``). A ``retrieval_log`` of the audit shape belongs to the
    site node and is left alone, as is a table of unrecognised shape. Nothing
    happens once ``rag_generation_log`` exists.
    """
    if _table_columns(cur, GENERATION_LOG_TABLE):
        return False
    legacy = _table_columns(cur, LEGACY_GENERATION_LOG_TABLE)
    if not legacy:
        return False
    if not _GENERATION_LOG_MARKERS.issubset(legacy) or _AUDIT_LOG_MARKERS & legacy:
        log.info(
            "Leaving %s alone: not the generation-quality shape (columns: %s)",
            LEGACY_GENERATION_LOG_TABLE,
            ", ".join(sorted(legacy)),
        )
        return False
    cur.execute(f"ALTER TABLE {LEGACY_GENERATION_LOG_TABLE} RENAME TO {GENERATION_LOG_TABLE}")
    cur.execute(
        f"ALTER INDEX IF EXISTS {_LEGACY_GENERATION_LOG_INDEX} RENAME TO {_GENERATION_LOG_INDEX}"
    )
    log.info(
        "Renamed legacy generation-quality table %s to %s (rows preserved)",
        LEGACY_GENERATION_LOG_TABLE,
        GENERATION_LOG_TABLE,
    )
    return True


def ensure_generation_schema(cur) -> None:
    """Bootstrap the generation tables on ``cur``: migrate first, then DDL.

    Order matters. The legacy rename must run before ``CREATE TABLE IF NOT
    EXISTS rag_generation_log``; a fresh empty table created first would
    strand the legacy rows behind the old name for good. A failed rename
    (e.g. ``lock_timeout``) therefore raises before any DDL runs, and the
    next ``connect()`` retries it.
    """
    migrate_legacy_generation_log(cur)
    cur.execute(_GENERATION_CONFIG_DDL)
    cur.execute(_RAG_GENERATION_LOG_DDL)


class GenerationManager:
    """Manages blue/green RAG generations per corpus tier."""

    def __init__(self, store) -> None:
        self._store = store
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        """Bootstrap the generation tables (idempotent; ``RAGStore.connect()`` does it too)."""
        try:
            conn = self._store._conn
            if conn is None:
                return
            with conn.cursor() as cur:
                ensure_generation_schema(cur)
        except Exception as exc:  # noqa: BLE001 — DB may be unavailable; never break construction
            log.debug("Generation schema bootstrap skipped: %s", exc)

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
