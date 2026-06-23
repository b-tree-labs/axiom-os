# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""PostgreSQL / pgvector storage for RAG chunks.

Three-tier corpus model:
  rag-community  — pre-indexed community knowledge (ships with pip package)
  rag-org        — facility/organization corpus (admin-managed)
  rag-internal   — personal workspace index (built during install + post-push)

Uses ``psycopg2`` for database access.  The schema is created automatically
on first ``connect()`` call if it does not exist.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import psycopg2
import psycopg2.extras

from .chunker import Chunk

log = logging.getLogger(__name__)

CORPUS_COMMUNITY = "rag-community"
CORPUS_ORG = "rag-org"
CORPUS_INTERNAL = "rag-internal"

ALL_CORPORA = (CORPUS_COMMUNITY, CORPUS_ORG, CORPUS_INTERNAL)

# ---------------------------------------------------------------------------
# Schema DDL — idempotent (IF NOT EXISTS everywhere)
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """\
-- Extensions must be created by superuser (done by setup-<host>.sh)
-- CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS documents (
    id              BIGSERIAL PRIMARY KEY,
    source_path     TEXT NOT NULL,
    corpus          TEXT NOT NULL DEFAULT 'rag-internal',
    source_type     TEXT NOT NULL DEFAULT 'markdown',
    title           TEXT NOT NULL DEFAULT '',
    checksum        TEXT NOT NULL DEFAULT '',
    content_hash    TEXT NOT NULL DEFAULT '',
    chunk_count     INTEGER NOT NULL DEFAULT 0,
    owner           TEXT,
    data_source     TEXT NOT NULL DEFAULT 'local',
    sync_id         TEXT NOT NULL DEFAULT '',
    corpus_generation INTEGER NOT NULL DEFAULT 1,
    graph_extracted_at TIMESTAMPTZ,
    first_indexed   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_indexed    TIMESTAMPTZ NOT NULL DEFAULT now(),
    access_tier          TEXT NOT NULL DEFAULT 'public',
    classification       TEXT NOT NULL DEFAULT 'unclassified',
    allowed_nationalities TEXT[],  -- NULL = unrestricted
    UNIQUE (source_path, corpus)
);

CREATE INDEX IF NOT EXISTS idx_documents_content_hash
    ON documents (content_hash) WHERE content_hash != '';

CREATE TABLE IF NOT EXISTS chunks (
    id              BIGSERIAL PRIMARY KEY,
    source_path     TEXT NOT NULL,
    source_title    TEXT NOT NULL DEFAULT '',
    source_type     TEXT NOT NULL DEFAULT 'markdown',
    chunk_text      TEXT NOT NULL,
    chunk_index     INTEGER NOT NULL DEFAULT 0,
    start_line      INTEGER NOT NULL DEFAULT 1,
    embedding       vector(768),
    corpus          TEXT NOT NULL DEFAULT 'rag-internal',
    owner           TEXT,
    team            TEXT,
    checksum        TEXT NOT NULL DEFAULT '',
    chunking_tier   TEXT NOT NULL DEFAULT 'fixed',
    corpus_generation INTEGER NOT NULL DEFAULT 1,
    indexed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    access_tier          TEXT NOT NULL DEFAULT 'public',
    classification       TEXT NOT NULL DEFAULT 'unclassified',
    cognitive_type        TEXT,  -- ADR-069: MIRIX type of a projected fragment (NULL for ingested docs)
    fragment_ref          TEXT,  -- ADR-069: source MemoryFragment id (NULL for ingested docs)
    allowed_nationalities TEXT[]  -- NULL = unrestricted
);

-- Idempotent upgrade for installs that pre-date these columns.
ALTER TABLE chunks    ADD COLUMN IF NOT EXISTS access_tier          TEXT NOT NULL DEFAULT 'public';
ALTER TABLE chunks    ADD COLUMN IF NOT EXISTS classification       TEXT NOT NULL DEFAULT 'unclassified';
ALTER TABLE chunks    ADD COLUMN IF NOT EXISTS allowed_nationalities TEXT[];
ALTER TABLE chunks    ADD COLUMN IF NOT EXISTS cognitive_type        TEXT;
ALTER TABLE chunks    ADD COLUMN IF NOT EXISTS fragment_ref          TEXT;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS access_tier          TEXT NOT NULL DEFAULT 'public';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS classification       TEXT NOT NULL DEFAULT 'unclassified';
ALTER TABLE documents ADD COLUMN IF NOT EXISTS allowed_nationalities TEXT[];

CREATE INDEX IF NOT EXISTS idx_chunks_access_tier    ON chunks (access_tier);
CREATE INDEX IF NOT EXISTS idx_chunks_classification ON chunks (classification);
CREATE INDEX IF NOT EXISTS idx_chunks_cognitive_type ON chunks (cognitive_type);
CREATE INDEX IF NOT EXISTS idx_chunks_fragment_ref   ON chunks (fragment_ref);

CREATE INDEX IF NOT EXISTS idx_chunks_source_path ON chunks (source_path);
CREATE INDEX IF NOT EXISTS idx_chunks_corpus ON chunks (corpus);
CREATE INDEX IF NOT EXISTS idx_chunks_generation ON chunks (corpus, corpus_generation);
CREATE INDEX IF NOT EXISTS idx_chunks_chunking_tier ON chunks (chunking_tier);
CREATE INDEX IF NOT EXISTS idx_chunks_embedding ON chunks
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
"""


@dataclass
class SearchResult:
    """A single search hit from the RAG store."""

    source_path: str
    source_title: str
    chunk_text: str
    chunk_index: int
    similarity: float
    combined_score: float
    corpus: str = CORPUS_INTERNAL


class RAGStore:
    """PostgreSQL/pgvector document store for RAG retrieval — three-tier corpus."""

    def __init__(self, database_url: str) -> None:
        self._dsn = database_url
        self._conn: psycopg2.extensions.connection | None = None

    # -- connection management ------------------------------------------------

    def connect(self) -> None:
        """Establish a database connection and ensure the schema exists."""
        if self._conn and not self._conn.closed:
            return
        self._conn = psycopg2.connect(self._dsn)
        self._conn.autocommit = True
        with self._conn.cursor() as cur:
            # Schema migration runs ALTER TABLE, which takes AccessExclusiveLock.
            # With no bound, a blocked ALTER waits forever on a concurrent reader
            # AND stalls the lock queue behind it — every later query then hangs
            # (observed 2026-06-08: a wedged migration froze all RAG reads). Bound
            # the lock wait so a contended migration fails fast instead of wedging.
            cur.execute("SET lock_timeout = '10s'")
            cur.execute(_SCHEMA_SQL)
            # Ensure generation infrastructure tables exist
            try:
                from axiom.rag.generation import _GENERATION_CONFIG_DDL, _RETRIEVAL_LOG_DDL

                cur.execute(_GENERATION_CONFIG_DDL)
                cur.execute(_RETRIEVAL_LOG_DDL)
            except Exception:
                pass  # Generation module may not be available
            # Ensure interaction log table exists
            try:
                from axiom.rag.interaction_log import ensure_interaction_log

                ensure_interaction_log(self._conn)
            except Exception:
                pass
            # Ensure retrieval audit table exists (T0-1)
            try:
                from axiom.rag.retrieval_audit import ensure_retrieval_audit

                ensure_retrieval_audit(self._conn)
            except Exception:
                pass
        log.info("RAGStore connected and schema ensured")

    def _cur(self):
        """Return a cursor, reconnecting if needed."""
        if self._conn is None or self._conn.closed:
            self.connect()
        assert self._conn is not None
        return self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()

    # -- write operations -----------------------------------------------------

    def upsert_chunks(
        self,
        chunks: list[Chunk],
        embeddings: list[list[float]] | None = None,
        checksum: str = "",
        content_hash: str = "",
        corpus: str = CORPUS_INTERNAL,
        owner: str | None = None,
        chunking_tier: str = "fixed",
        data_source: str = "local",
        sync_id: str = "",
        corpus_generation: int = 1,
        cognitive_type: str | None = None,
        fragment_ref: str | None = None,
    ) -> None:
        """Insert or replace all chunks for a document within a corpus.

        All chunks must share the same ``source_path``.  Existing chunks for
        that path+corpus are deleted first (full replace).  Embeddings are
        optional — if None, chunks are stored with NULL embeddings (full-text
        search only).
        """
        if not chunks:
            return

        source_path = chunks[0].source_path
        now = datetime.now(UTC)

        with self._cur() as cur:
            # Delete old chunks for this path+corpus
            cur.execute(
                "DELETE FROM chunks WHERE source_path = %s AND corpus = %s",
                (source_path, corpus),
            )

            for i, chunk in enumerate(chunks):
                emb = embeddings[i] if embeddings and i < len(embeddings) else None
                emb_val = str(emb) if emb else None
                cur.execute(
                    """
                    INSERT INTO chunks
                        (source_path, source_title, source_type, chunk_text,
                         chunk_index, start_line, embedding, corpus, owner,
                         checksum, indexed_at, updated_at,
                         cognitive_type, fragment_ref)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s)
                    """,
                    (
                        chunk.source_path,
                        chunk.source_title,
                        chunk.source_type,
                        chunk.text,
                        chunk.chunk_index,
                        chunk.start_line,
                        emb_val,
                        corpus,
                        owner,
                        checksum,
                        now,
                        now,
                        cognitive_type,
                        fragment_ref,
                    ),
                )

            # Upsert documents record
            cur.execute(
                """
                INSERT INTO documents
                    (source_path, corpus, source_type, title, checksum,
                     chunk_count, owner, first_indexed, last_indexed)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_path, corpus) DO UPDATE SET
                    title        = EXCLUDED.title,
                    checksum     = EXCLUDED.checksum,
                    chunk_count  = EXCLUDED.chunk_count,
                    owner        = EXCLUDED.owner,
                    last_indexed = EXCLUDED.last_indexed
                """,
                (
                    source_path,
                    corpus,
                    chunks[0].source_type,
                    chunks[0].source_title,
                    checksum,
                    len(chunks),
                    owner,
                    now,
                    now,
                ),
            )

    def delete_document(self, path: str, corpus: str = CORPUS_INTERNAL) -> None:
        """Remove all chunks and the document record for *path* in *corpus*."""
        with self._cur() as cur:
            cur.execute(
                "DELETE FROM chunks WHERE source_path = %s AND corpus = %s",
                (path, corpus),
            )
            cur.execute(
                "DELETE FROM documents WHERE source_path = %s AND corpus = %s",
                (path, corpus),
            )

    def delete_corpus(self, corpus: str) -> int:
        """Remove all chunks and documents for an entire corpus. Returns chunk count."""
        with self._cur() as cur:
            cur.execute("SELECT count(*) AS n FROM chunks WHERE corpus = %s", (corpus,))
            row = cur.fetchone()
            assert row is not None
            n = row["n"]
            cur.execute("DELETE FROM chunks WHERE corpus = %s", (corpus,))
            cur.execute("DELETE FROM documents WHERE corpus = %s", (corpus,))
        log.info("Deleted corpus %s (%d chunks)", corpus, n)
        return n

    # -- read operations ------------------------------------------------------

    def get_document(self, path: str, corpus: str = CORPUS_INTERNAL) -> dict | None:
        """Return document metadata or ``None`` if not indexed."""
        with self._cur() as cur:
            cur.execute(
                "SELECT source_path, corpus, checksum, content_hash, chunk_count, last_indexed "
                "FROM documents WHERE source_path = %s AND corpus = %s",
                (path, corpus),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def find_documents_by_name(self, name: str, corpus: str = CORPUS_INTERNAL) -> list[dict]:
        """Resolve a name-or-path to matching documents in *corpus*.

        If ``name`` contains a ``/``, treat as an exact ``source_path``
        match. Otherwise, basename-match (``source_path = name`` or
        ``source_path LIKE '%/<name>'``). Returns a list of
        ``{source_path, chunk_count}`` dicts so the caller can show
        candidates + sizes before deleting. See ``axi rag remove``.
        """
        with self._cur() as cur:
            if "/" in name:
                cur.execute(
                    "SELECT source_path, chunk_count "
                    "FROM documents WHERE source_path = %s AND corpus = %s",
                    (name, corpus),
                )
            else:
                # Match either an exact bare-filename row OR any path
                # whose final component equals ``name``. The trailing-
                # slash pattern keeps ``foo.pdf`` from matching
                # ``otherfoo.pdf``.
                like = f"%/{name}"
                cur.execute(
                    "SELECT source_path, chunk_count "
                    "FROM documents "
                    "WHERE corpus = %s AND (source_path = %s OR source_path LIKE %s) "
                    "ORDER BY source_path",
                    (corpus, name, like),
                )
            return [dict(row) for row in cur.fetchall()]

    def list_document_paths(self, corpus: str = CORPUS_INTERNAL) -> list[str]:
        """Return all indexed source paths in *corpus* (for auditing / purge)."""
        with self._cur() as cur:
            cur.execute(
                "SELECT source_path FROM documents WHERE corpus = %s ORDER BY source_path",
                (corpus,),
            )
            return [row["source_path"] for row in cur.fetchall()]

    def find_by_content_hash(self, content_hash: str) -> list[dict]:
        """Find all documents with the same extracted-text content hash."""
        if not content_hash:
            return []
        with self._cur() as cur:
            cur.execute(
                "SELECT source_path, corpus, checksum, content_hash, chunk_count, last_indexed "
                "FROM documents WHERE content_hash = %s",
                (content_hash,),
            )
            return [dict(r) for r in cur.fetchall()]

    def search(
        self,
        query_embedding: list[float] | None = None,
        query_text: str = "",
        corpora: list[str] | None = None,
        limit: int = 5,
        chunking_tier: str | None = None,
        corpus_generation: int | None = None,
    ) -> list[SearchResult]:
        """Hybrid vector + full-text search across one or more corpora.

        Priority order: rag-internal > rag-org > rag-community.
        If ``corpora`` is None, searches all three in priority order.
        If no embeddings (query_embedding is None), falls back to pure
        full-text search over tsvector.
        """
        if corpora is None:
            corpora = list(ALL_CORPORA)

        corpus_filter = tuple(corpora)
        params: list = []

        if query_embedding is not None:
            if query_text.strip():
                sql = """
                    WITH vector_search AS (
                        SELECT id, source_path, source_title, chunk_text,
                               chunk_index, corpus,
                               1 - (embedding <=> %s::vector) AS similarity
                        FROM chunks
                        WHERE corpus = ANY(%s) AND embedding IS NOT NULL
                        ORDER BY embedding <=> %s::vector
                        LIMIT %s
                    ),
                    text_search AS (
                        SELECT id,
                               ts_rank(to_tsvector('english', chunk_text),
                                       websearch_to_tsquery('english', %s)) AS text_rank
                        FROM chunks
                        WHERE corpus = ANY(%s)
                          AND to_tsvector('english', chunk_text) @@
                              websearch_to_tsquery('english', %s)
                    )
                    SELECT v.source_path, v.source_title, v.chunk_text,
                           v.chunk_index, v.corpus, v.similarity,
                           (0.7 * v.similarity + 0.3 * COALESCE(t.text_rank, 0))
                               AS combined_score
                    FROM vector_search v
                    LEFT JOIN text_search t ON v.id = t.id
                    ORDER BY combined_score DESC
                    LIMIT %s
                """
                emb_str = str(query_embedding)
                params = [
                    emb_str,
                    list(corpus_filter),
                    emb_str,
                    limit * 2,
                    query_text,
                    list(corpus_filter),
                    query_text,
                    limit,
                ]
            else:
                sql = """
                    SELECT source_path, source_title, chunk_text, chunk_index, corpus,
                           1 - (embedding <=> %s::vector) AS similarity,
                           1 - (embedding <=> %s::vector) AS combined_score
                    FROM chunks
                    WHERE corpus = ANY(%s) AND embedding IS NOT NULL
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                """
                emb_str = str(query_embedding)
                params = [emb_str, emb_str, list(corpus_filter), emb_str, limit]
        else:
            # Pure full-text search
            sql = """
                SELECT source_path, source_title, chunk_text, chunk_index, corpus,
                       0.0 AS similarity,
                       ts_rank(to_tsvector('english', chunk_text),
                               websearch_to_tsquery('english', %s)) AS combined_score
                FROM chunks
                WHERE corpus = ANY(%s)
                  AND to_tsvector('english', chunk_text) @@
                      websearch_to_tsquery('english', %s)
                ORDER BY combined_score DESC
                LIMIT %s
            """
            params = [query_text, list(corpus_filter), query_text, limit]

        with self._cur() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

        results = []
        for r in rows:
            chunk_text = r["chunk_text"]
            try:
                from axiom.infra.security_log import SecurityLog
                from axiom.infra.trace import current_session
                from axiom.rag.sanitizer import get_sanitizer

                clean_text, hits = get_sanitizer().sanitize(chunk_text)
                if hits:
                    SecurityLog.get().chunk_injection(
                        chunk_source=r["source_path"],
                        patterns_matched=hits,
                        session_id=current_session(),
                        corpus=r["corpus"],
                        sanitized=True,
                    )
                    chunk_text = clean_text
            except Exception:
                pass  # sanitization never blocks retrieval
            results.append(
                SearchResult(
                    source_path=r["source_path"],
                    source_title=r["source_title"],
                    chunk_text=chunk_text,
                    chunk_index=r["chunk_index"],
                    similarity=float(r["similarity"]),
                    combined_score=float(r["combined_score"]),
                    corpus=r["corpus"],
                )
            )
        return results

    def stats(self) -> dict:
        """Return index statistics including per-corpus breakdown."""
        with self._cur() as cur:
            cur.execute("SELECT count(*) AS n FROM documents")
            row = cur.fetchone()
            assert row is not None
            total_docs = row["n"]

            cur.execute("SELECT count(*) AS n FROM chunks")
            row = cur.fetchone()
            assert row is not None
            total_chunks = row["n"]

            cur.execute("SELECT corpus, count(*) AS n FROM chunks GROUP BY corpus ORDER BY corpus")
            by_corpus = {r["corpus"]: r["n"] for r in cur.fetchall()}

            cur.execute(
                "SELECT corpus, count(*) AS n FROM documents GROUP BY corpus ORDER BY corpus"
            )
            docs_by_corpus = {r["corpus"]: r["n"] for r in cur.fetchall()}

        return {
            "total_documents": total_docs,
            "total_chunks": total_chunks,
            "chunks_by_corpus": by_corpus,
            "documents_by_corpus": docs_by_corpus,
        }

    # -- community corpus operations ------------------------------------------

    def load_community_dump(self, dump_path: Path) -> None:
        """Load a pre-built community corpus from a pg_dump file.

        Clears the existing rag-community corpus first, then restores.
        The dump must contain only the chunks/documents tables filtered to
        corpus='rag-community'.

        Args:
            dump_path: Path to the .sql or .pgdump file to restore.
        """
        log.info("Loading community corpus from %s", dump_path)

        # Clear existing community data
        deleted = self.delete_corpus(CORPUS_COMMUNITY)
        if deleted > 0:
            log.info("Cleared %d existing community chunks before reload", deleted)

        # Use psql to execute the dump directly
        cmd = ["psql", self._dsn, "-f", str(dump_path)]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            log.info("Community corpus loaded: %s", result.stdout.strip() or "ok")
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"Failed to load community corpus from {dump_path}:\n{exc.stderr}"
            ) from exc
        except FileNotFoundError:
            raise RuntimeError("psql not found — ensure PostgreSQL client tools are installed")

    def load_pack_csv(self, pack_dir: Path) -> dict:
        """Load a community knowledge pack from CSV files.

        Expects ``chunks.csv`` and ``documents.csv`` in *pack_dir*.
        Clears the existing community corpus first, then bulk-loads.
        Returns dict with loaded counts.
        """
        import csv

        chunks_csv = pack_dir / "chunks.csv"
        docs_csv = pack_dir / "documents.csv"
        if not chunks_csv.exists() or not docs_csv.exists():
            raise FileNotFoundError(f"Pack at {pack_dir} missing chunks.csv or documents.csv")

        log.info("Loading community pack from %s", pack_dir)
        deleted = self.delete_corpus(CORPUS_COMMUNITY)
        if deleted > 0:
            log.info("Cleared %d existing community chunks", deleted)

        assert self._conn is not None
        cur = self._conn.cursor()

        # Load documents
        with open(docs_csv, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            doc_count = 0
            for row in reader:
                cur.execute(
                    """INSERT INTO documents
                        (source_path, corpus, source_type, title, checksum,
                         content_hash, chunk_count, owner, data_source, sync_id,
                         first_indexed, last_indexed)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (source_path, corpus) DO UPDATE SET
                        chunk_count=EXCLUDED.chunk_count, last_indexed=EXCLUDED.last_indexed""",
                    (
                        row["source_path"],
                        CORPUS_COMMUNITY,
                        row.get("source_type", "pdf"),
                        row.get("title", ""),
                        row.get("checksum", ""),
                        row.get("content_hash", ""),
                        int(row.get("chunk_count", 0)),
                        row.get("owner") or None,
                        row.get("data_source", "pack"),
                        row.get("sync_id", ""),
                        row.get("first_indexed", datetime.now(UTC)),
                        row.get("last_indexed", datetime.now(UTC)),
                    ),
                )
                doc_count += 1

        # Load chunks in batches
        now_str = datetime.now(UTC).isoformat()
        with open(chunks_csv, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            chunk_count = 0
            batch = []
            for row in reader:
                emb = row.get("embedding", "")
                emb_val = emb if emb and emb not in ("None", "null", "") else None
                batch.append(
                    (
                        row["source_path"],
                        row.get("source_title", ""),
                        row.get("source_type", "pdf"),
                        row["chunk_text"],
                        int(row.get("chunk_index", 0)),
                        int(row.get("start_line", 1)),
                        emb_val,
                        CORPUS_COMMUNITY,
                        row.get("owner") or None,
                        row.get("team") or None,
                        row.get("checksum", ""),
                        row.get("chunking_tier", "fixed"),
                        row.get("indexed_at", now_str),
                        row.get("updated_at", now_str),
                    )
                )
                chunk_count += 1
                if len(batch) >= 500:
                    psycopg2.extras.execute_batch(
                        cur,
                        """
                        INSERT INTO chunks (source_path, source_title, source_type,
                            chunk_text, chunk_index, start_line, embedding, corpus,
                            owner, team, checksum, chunking_tier, indexed_at, updated_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        batch,
                    )
                    batch = []
            if batch:
                psycopg2.extras.execute_batch(
                    cur,
                    """
                    INSERT INTO chunks (source_path, source_title, source_type,
                        chunk_text, chunk_index, start_line, embedding, corpus,
                        owner, team, checksum, chunking_tier, indexed_at, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    batch,
                )

        self._conn.commit()

        # Warn if chunks lack embeddings
        with self._cur() as cur2:
            cur2.execute(
                "SELECT count(*) AS n FROM chunks WHERE corpus = %s AND embedding IS NULL",
                (CORPUS_COMMUNITY,),
            )
            missing = cur2.fetchone()["n"]
            if missing > 0:
                log.warning(
                    "%d/%d community chunks have no embeddings — run embedding pipeline to fix",
                    missing,
                    chunk_count,
                )

        log.info("Loaded %d documents, %d chunks from pack", doc_count, chunk_count)
        return {"documents": doc_count, "chunks": chunk_count}
