# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""SQLite + FTS5 + sqlite-vec storage for RAG chunks.

Drop-in replacement for RAGStore when PostgreSQL is not available.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .chunker import Chunk
from .store import (
    ALL_CORPORA,
    CORPUS_INTERNAL,
    SearchResult,
)

log = logging.getLogger(__name__)

_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS documents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
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
    graph_extracted_at TEXT,
    first_indexed   TEXT NOT NULL DEFAULT '',
    last_indexed    TEXT NOT NULL DEFAULT '',
    access_tier          TEXT NOT NULL DEFAULT 'public',
    classification       TEXT NOT NULL DEFAULT 'unclassified',
    allowed_nationalities TEXT,  -- JSON array; NULL = unrestricted
    source_url      TEXT,          -- ADR-091: origin system's shareable link
    source_ref_id   TEXT,          -- ADR-091: origin system's stable id
    UNIQUE (source_path, corpus)
);

CREATE INDEX IF NOT EXISTS idx_documents_content_hash
    ON documents (content_hash);

CREATE TABLE IF NOT EXISTS chunks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path     TEXT NOT NULL,
    source_title    TEXT NOT NULL DEFAULT '',
    source_type     TEXT NOT NULL DEFAULT 'markdown',
    chunk_text      TEXT NOT NULL,
    chunk_index     INTEGER NOT NULL DEFAULT 0,
    start_line      INTEGER NOT NULL DEFAULT 1,
    corpus          TEXT NOT NULL DEFAULT 'rag-internal',
    owner           TEXT,
    team            TEXT,
    checksum        TEXT NOT NULL DEFAULT '',
    chunking_tier   TEXT NOT NULL DEFAULT 'fixed',
    corpus_generation INTEGER NOT NULL DEFAULT 1,
    indexed_at      TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL DEFAULT '',
    access_tier          TEXT NOT NULL DEFAULT 'public',
    classification       TEXT NOT NULL DEFAULT 'unclassified',
    cognitive_type        TEXT,  -- ADR-069: MIRIX type of a projected fragment (NULL for ingested docs)
    fragment_ref          TEXT,  -- ADR-069: source MemoryFragment id (NULL for ingested docs)
    allowed_nationalities TEXT  -- JSON array; NULL = unrestricted
);

CREATE INDEX IF NOT EXISTS idx_chunks_source_path ON chunks (source_path);
CREATE INDEX IF NOT EXISTS idx_chunks_corpus ON chunks (corpus);
CREATE INDEX IF NOT EXISTS idx_chunks_generation ON chunks (corpus, corpus_generation);
"""

_FTS_SQL = """\
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    chunk_text,
    content='chunks',
    content_rowid='id',
    tokenize='porter unicode61'
);
"""

_ACCESS_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("access_tier", "TEXT NOT NULL DEFAULT 'public'", "public"),
    ("classification", "TEXT NOT NULL DEFAULT 'unclassified'", "unclassified"),
    ("allowed_nationalities", "TEXT", None),
)


# ADR-069: chunks-only (NOT documents — mirrors the PG side, which adds these
# to chunks alone). Keeps the two backends in column parity.
_CHUNK_PROJECTION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("cognitive_type", "TEXT"),
    ("fragment_ref", "TEXT"),
)


def _migrate_access_columns(conn: sqlite3.Connection) -> None:
    """Idempotently add access-control columns to existing chunks/documents."""
    for table in ("chunks", "documents"):
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, ddl, _default in _ACCESS_COLUMNS:
            if name in existing:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    # ADR-069 projection columns: chunks only.
    chunk_cols = {row[1] for row in conn.execute("PRAGMA table_info(chunks)").fetchall()}
    for name, ddl in _CHUNK_PROJECTION_COLUMNS:
        if name not in chunk_cols:
            conn.execute(f"ALTER TABLE chunks ADD COLUMN {name} {ddl}")
    # ADR-091 shareable-URL provenance columns: documents only.
    doc_cols = {row[1] for row in conn.execute("PRAGMA table_info(documents)").fetchall()}
    for name in ("source_url", "source_ref_id"):
        if name not in doc_cols:
            conn.execute(f"ALTER TABLE documents ADD COLUMN {name} TEXT")
    conn.commit()


_FTS_TRIGGERS = """\
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, chunk_text) VALUES (new.id, new.chunk_text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, chunk_text) VALUES('delete', old.id, old.chunk_text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, chunk_text) VALUES('delete', old.id, old.chunk_text);
    INSERT INTO chunks_fts(rowid, chunk_text) VALUES (new.id, new.chunk_text);
END;
"""


class SQLiteRAGStore:
    """SQLite/FTS5/sqlite-vec document store for RAG retrieval."""

    def __init__(self, database_url: str) -> None:
        if database_url.startswith("sqlite:///"):
            self._db_path = database_url[len("sqlite:///") :]
        elif database_url.startswith("sqlite://"):
            self._db_path = database_url[len("sqlite://") :]
        else:
            self._db_path = database_url
        self._conn: sqlite3.Connection | None = None
        self._vec_available = False
        self._vec_table_created = False

    def connect(self) -> None:
        if self._conn is not None:
            return
        db_path = Path(self._db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")

        self._conn.executescript(_SCHEMA_SQL)
        _migrate_access_columns(self._conn)
        self._conn.executescript(_FTS_SQL)
        self._conn.executescript(_FTS_TRIGGERS)

        try:
            import sqlite_vec

            self._conn.enable_load_extension(True)
            sqlite_vec.load(self._conn)
            self._conn.enable_load_extension(False)
            self._vec_available = True
        except Exception:
            self._vec_available = False

        self._conn.commit()
        log.info("SQLiteRAGStore connected at %s (vec=%s)", self._db_path, self._vec_available)

    def _ensure_vec_table(self, dim: int) -> None:
        if not self._vec_available or self._vec_table_created:
            return
        assert self._conn is not None
        self._conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0("
            f"  chunk_id INTEGER PRIMARY KEY, embedding float[{dim}])"
        )
        self._conn.commit()
        self._vec_table_created = True

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # -- write ----------------------------------------------------------------

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
        source_url: str | None = None,
        source_ref_id: str | None = None,
    ) -> None:
        if not chunks:
            return
        assert self._conn is not None
        source_path = chunks[0].source_path
        now = datetime.now(UTC).isoformat()

        # Delete old chunks for this path+corpus
        old_ids = [
            row[0]
            for row in self._conn.execute(
                "SELECT id FROM chunks WHERE source_path = ? AND corpus = ?",
                (source_path, corpus),
            ).fetchall()
        ]
        self._conn.execute(
            "DELETE FROM chunks WHERE source_path = ? AND corpus = ?",
            (source_path, corpus),
        )
        if self._vec_available and self._vec_table_created and old_ids:
            for oid in old_ids:
                self._conn.execute("DELETE FROM chunks_vec WHERE chunk_id = ?", (oid,))

        if embeddings and len(embeddings) > 0:
            self._ensure_vec_table(len(embeddings[0]))

        for i, chunk in enumerate(chunks):
            cur = self._conn.execute(
                "INSERT INTO chunks (source_path, source_title, source_type, chunk_text,"
                " chunk_index, start_line, corpus, owner, checksum, chunking_tier,"
                " indexed_at, updated_at, cognitive_type, fragment_ref)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    chunk.source_path,
                    chunk.source_title,
                    chunk.source_type,
                    chunk.text,
                    chunk.chunk_index,
                    chunk.start_line,
                    corpus,
                    owner,
                    checksum,
                    chunking_tier,
                    now,
                    now,
                    cognitive_type,
                    fragment_ref,
                ),
            )
            if embeddings and i < len(embeddings) and self._vec_available:
                chunk_id = cur.lastrowid
                emb_json = json.dumps(embeddings[i])
                self._conn.execute(
                    "INSERT INTO chunks_vec (chunk_id, embedding) VALUES (?, ?)",
                    (chunk_id, emb_json),
                )

        self._conn.execute(
            "INSERT INTO documents (source_path, corpus, source_type, title, checksum,"
            " content_hash, chunk_count, owner, data_source, sync_id,"
            " first_indexed, last_indexed, source_url, source_ref_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT (source_path, corpus) DO UPDATE SET"
            "   title=excluded.title, checksum=excluded.checksum,"
            "   content_hash=excluded.content_hash, chunk_count=excluded.chunk_count,"
            "   owner=excluded.owner, data_source=excluded.data_source,"
            "   sync_id=excluded.sync_id, last_indexed=excluded.last_indexed,"
            "   source_url=COALESCE(excluded.source_url, documents.source_url),"
            "   source_ref_id=COALESCE(excluded.source_ref_id, documents.source_ref_id)",
            (
                source_path,
                corpus,
                chunks[0].source_type,
                chunks[0].source_title,
                checksum,
                content_hash,
                len(chunks),
                owner,
                data_source,
                sync_id,
                now,
                now,
                source_url,
                source_ref_id,
            ),
        )
        self._conn.commit()

    def delete_document(self, path: str, corpus: str = CORPUS_INTERNAL) -> None:
        assert self._conn is not None
        self._conn.execute(
            "DELETE FROM chunks WHERE source_path = ? AND corpus = ?", (path, corpus)
        )
        self._conn.execute(
            "DELETE FROM documents WHERE source_path = ? AND corpus = ?", (path, corpus)
        )
        self._conn.commit()

    def delete_corpus(self, corpus: str) -> int:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT count(*) FROM chunks WHERE corpus = ?", (corpus,)
        ).fetchone()
        n = row[0] if row else 0
        self._conn.execute("DELETE FROM chunks WHERE corpus = ?", (corpus,))
        self._conn.execute("DELETE FROM documents WHERE corpus = ?", (corpus,))
        self._conn.commit()
        return n

    # -- read -----------------------------------------------------------------

    def get_document(self, path: str, corpus: str = CORPUS_INTERNAL) -> dict | None:
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT source_path, corpus, checksum, content_hash, chunk_count, last_indexed,"
            " source_url, source_ref_id"
            " FROM documents WHERE source_path = ? AND corpus = ?",
            (path, corpus),
        ).fetchone()
        return dict(row) if row else None

    def find_by_content_hash(self, content_hash: str) -> list[dict]:
        if not content_hash:
            return []
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT source_path, corpus, checksum, content_hash, chunk_count, last_indexed"
            " FROM documents WHERE content_hash = ?",
            (content_hash,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- search ---------------------------------------------------------------

    def search(
        self,
        query_embedding: list[float] | None = None,
        query_text: str = "",
        corpora: list[str] | None = None,
        limit: int = 5,
        chunking_tier: str | None = None,
        corpus_generation: int | None = None,
    ) -> list[SearchResult]:
        if corpora is None:
            corpora = list(ALL_CORPORA)
        assert self._conn is not None

        placeholders = ",".join("?" for _ in corpora)
        tier_clause = ""
        tier_params: list = []
        if chunking_tier:
            tier_clause = " AND c.chunking_tier = ?"
            tier_params = [chunking_tier]

        results: list[SearchResult] = []

        # Vector search if we have embeddings + vec extension
        if query_embedding is not None and self._vec_available and self._vec_table_created:
            emb_json = json.dumps(query_embedding)
            vec_sql = (
                "SELECT c.source_path, c.source_title, c.chunk_text,"
                " c.chunk_index, c.corpus, c.id, v.distance"
                " FROM chunks_vec v"
                " JOIN chunks c ON c.id = v.chunk_id"
                " WHERE v.embedding MATCH ? AND k = ?"
            )
            rows = self._conn.execute(vec_sql, [emb_json, limit * 3]).fetchall()

            # Post-filter by corpus and tier
            filtered = []
            for r in rows:
                if r["corpus"] not in corpora:
                    continue
                if chunking_tier:
                    # Need to check chunking_tier from chunks table
                    tier_row = self._conn.execute(
                        "SELECT chunking_tier FROM chunks WHERE id = ?", (r["id"],)
                    ).fetchone()
                    if tier_row and tier_row[0] != chunking_tier:
                        continue
                filtered.append(r)

            for r in filtered[:limit]:
                similarity = max(0.0, 1.0 - float(r["distance"]))
                results.append(
                    SearchResult(
                        source_path=r["source_path"],
                        source_title=r["source_title"],
                        chunk_text=r["chunk_text"],
                        chunk_index=r["chunk_index"],
                        similarity=similarity,
                        combined_score=similarity,
                        corpus=r["corpus"],
                    )
                )

        # Full-text search fallback
        if not results and query_text.strip():
            fts_sql = (
                f"SELECT c.source_path, c.source_title, c.chunk_text,"
                f" c.chunk_index, c.corpus, rank AS fts_rank"
                f" FROM chunks_fts f"
                f" JOIN chunks c ON c.id = f.rowid"
                f" WHERE chunks_fts MATCH ?"
                f"   AND c.corpus IN ({placeholders}){tier_clause}"
                f" ORDER BY rank"
                f" LIMIT ?"
            )
            try:
                rows = self._conn.execute(
                    fts_sql, [query_text, *corpora, *tier_params, limit]
                ).fetchall()
            except sqlite3.OperationalError:
                simple_q = " ".join(w for w in query_text.split() if w.isalnum())
                if simple_q:
                    rows = self._conn.execute(
                        fts_sql, [simple_q, *corpora, *tier_params, limit]
                    ).fetchall()
                else:
                    rows = []

            for r in rows:
                results.append(
                    SearchResult(
                        source_path=r["source_path"],
                        source_title=r["source_title"],
                        chunk_text=r["chunk_text"],
                        chunk_index=r["chunk_index"],
                        similarity=0.0,
                        combined_score=abs(float(r["fts_rank"])),
                        corpus=r["corpus"],
                    )
                )

        return results

    # -- stats ----------------------------------------------------------------

    def stats(self) -> dict:
        assert self._conn is not None
        total_docs = self._conn.execute("SELECT count(*) FROM documents").fetchone()[0]
        total_chunks = self._conn.execute("SELECT count(*) FROM chunks").fetchone()[0]

        by_corpus = {}
        for row in self._conn.execute(
            "SELECT corpus, count(*) AS n FROM chunks GROUP BY corpus"
        ).fetchall():
            by_corpus[row["corpus"]] = row["n"]

        docs_by_corpus = {}
        for row in self._conn.execute(
            "SELECT corpus, count(*) AS n FROM documents GROUP BY corpus"
        ).fetchall():
            docs_by_corpus[row["corpus"]] = row["n"]

        dup_row = self._conn.execute(
            "SELECT count(*) FROM ("
            "  SELECT content_hash FROM documents"
            "  WHERE content_hash != ''"
            "  GROUP BY content_hash HAVING count(*) > 1)"
        ).fetchone()
        dup_count = dup_row[0] if dup_row else 0

        return {
            "total_documents": total_docs,
            "total_chunks": total_chunks,
            "chunks_by_corpus": by_corpus,
            "documents_by_corpus": docs_by_corpus,
            "duplicate_content_groups": dup_count,
        }
