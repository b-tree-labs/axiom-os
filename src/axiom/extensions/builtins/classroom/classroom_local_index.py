# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Per-classroom local vector + graph index.

Phase 4 of the materials-flow tier. A student's ``~/.axi/classrooms/<id>/
index.db`` holds:

    - chunks + FTS5 full-text index (keyword search, always works)
    - chunks_vec — optional sqlite-vec table (semantic similarity,
      when sqlite-vec is installed AND an embedder is available)
    - graph_entities + graph_edges (structural context; entities
      extracted by :mod:`axiom.graph.extractors.deterministic`)

One SQLite file per classroom — small, portable, inspectable by an
instructor debugging a student's machine. Reuse of the platform's
sqlite_store / RPE is deliberately deferred; keep the classroom
surface self-contained while we learn what the combo feels like.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from axiom.graph.extractors.deterministic import extract_from_document

EmbedFn = Callable[[list[str]], list[list[float]]]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id     TEXT NOT NULL,
    title       TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    text        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_file_id ON chunks(file_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    content='chunks',
    content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS chunks_fts_insert AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_fts_delete AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text)
      VALUES ('delete', old.id, old.text);
END;

CREATE TABLE IF NOT EXISTS graph_entities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id     TEXT NOT NULL,
    label       TEXT NOT NULL,
    name        TEXT NOT NULL,
    properties  TEXT NOT NULL DEFAULT '{}',
    confidence  REAL NOT NULL DEFAULT 1.0
);
CREATE INDEX IF NOT EXISTS idx_graph_entities_file ON graph_entities(file_id);
CREATE INDEX IF NOT EXISTS idx_graph_entities_name ON graph_entities(name);

CREATE TABLE IF NOT EXISTS graph_edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id     TEXT NOT NULL,
    rel_type    TEXT NOT NULL,
    from_name   TEXT NOT NULL,
    from_label  TEXT NOT NULL,
    to_name     TEXT NOT NULL,
    to_label    TEXT NOT NULL,
    confidence  REAL NOT NULL DEFAULT 1.0
);
CREATE INDEX IF NOT EXISTS idx_graph_edges_file ON graph_edges(file_id);
"""


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchHit:
    file_id: str
    title: str
    text: str
    score: float


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


@dataclass
class ClassroomLocalIndex:
    base_dir: Path
    _conn: sqlite3.Connection | None = None
    _vec_available: bool = False
    _vec_dim: int | None = None

    @property
    def _db_path(self) -> Path:
        return self.base_dir / "index.db"

    # ---- Lifecycle ----

    def open(self) -> None:
        if self._conn is not None:
            return
        self.base_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)

        # Try to load sqlite-vec; if absent we just skip the vector path.
        try:
            import sqlite_vec

            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            self._vec_available = True
        except Exception:
            self._vec_available = False

        conn.commit()
        self._conn = conn

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    # ---- Ingest ----

    def ingest(
        self,
        *,
        file_id: str,
        title: str,
        content: str,
        embed: EmbedFn | None = None,
    ) -> None:
        """Ingest one document. Replaces prior ingestion for the same file_id.

        - Splits ``content`` into paragraph-ish chunks
        - Writes chunks + FTS5 entries
        - Optionally writes embeddings (if ``embed`` provided and sqlite-vec
          available)
        - Extracts graph entities + edges from the full document text
        """
        assert self._conn is not None, "call open() first"
        conn = self._conn

        # Purge prior state for this file_id (re-ingest supersedes).
        conn.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))
        conn.execute("DELETE FROM graph_entities WHERE file_id = ?", (file_id,))
        conn.execute("DELETE FROM graph_edges WHERE file_id = ?", (file_id,))

        chunks = _chunk_text(content, title=title)

        chunk_ids: list[int] = []
        for i, chunk_text in enumerate(chunks):
            cur = conn.execute(
                "INSERT INTO chunks (file_id, title, chunk_index, text) "
                "VALUES (?, ?, ?, ?)",
                (file_id, title, i, chunk_text),
            )
            chunk_ids.append(int(cur.lastrowid))

        # Optional vector path.
        if embed is not None and self._vec_available and chunks:
            vectors = embed(chunks)
            if vectors:
                dim = len(vectors[0])
                self._ensure_vec_table(dim)
                for cid, vec in zip(chunk_ids, vectors):
                    if len(vec) != dim:
                        # inconsistent dims — skip vector row; FTS still works.
                        continue
                    conn.execute(
                        "INSERT INTO chunks_vec (chunk_id, embedding) VALUES (?, ?)",
                        (cid, json.dumps(vec)),
                    )

        # Graph extraction (deterministic — no LLM).
        extracted = extract_from_document(
            text=content,
            source_path=file_id,
            source_type="markdown",
        )
        for ent in extracted.entities:
            conn.execute(
                "INSERT INTO graph_entities "
                "(file_id, label, name, properties, confidence) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    file_id,
                    ent.label,
                    ent.name,
                    json.dumps(ent.properties or {}),
                    float(ent.confidence),
                ),
            )
        for edge in extracted.edges:
            conn.execute(
                "INSERT INTO graph_edges "
                "(file_id, rel_type, from_name, from_label, to_name, to_label, confidence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    file_id,
                    edge.rel_type,
                    edge.from_name,
                    edge.from_label,
                    edge.to_name,
                    edge.to_label,
                    float(edge.confidence),
                ),
            )

        conn.commit()

    # ---- Search ----

    def search(self, query: str, k: int = 5) -> list[SearchHit]:
        assert self._conn is not None, "call open() first"
        conn = self._conn
        if not query.strip():
            return []

        # FTS5 keyword search. Naive phrase search ("control rod") won't
        # match "control rods" (no stemming). Strategy: strip non-alnum,
        # drop very short / stop-like tokens, OR the remaining prefixes
        # together, and let FTS5's rank order hits by relevance.
        #
        # OR (not AND) so that questions like "what is a control rod?"
        # still hit passages containing "control rods" even though
        # "what"/"is"/"a" don't appear. We rely on FTS5 rank to push
        # more-relevant chunks to the top.
        stopwords = {
            "the", "a", "an", "and", "or", "of", "to", "is", "are",
            "what", "who", "where", "why", "how", "when", "this", "that",
            "these", "those", "does", "do", "did", "in", "on", "for",
            "with", "by", "from", "as", "at", "be", "can",
        }
        raw_tokens = [
            "".join(ch for ch in tok if ch.isalnum() or ch == "_")
            for tok in query.lower().split()
        ]
        tokens = [
            t for t in raw_tokens
            if len(t) >= 2 and t not in stopwords
        ]
        if not tokens:
            return []
        fts_query = " OR ".join(f"{t}*" for t in tokens)
        try:
            rows = conn.execute(
                """
                SELECT c.file_id, c.title, c.text, fts.rank AS score
                FROM chunks_fts fts
                JOIN chunks c ON c.id = fts.rowid
                WHERE chunks_fts MATCH ?
                ORDER BY fts.rank
                LIMIT ?
                """,
                (fts_query, k),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        return [
            SearchHit(
                file_id=r["file_id"],
                title=r["title"],
                text=r["text"],
                score=float(r["score"]) if r["score"] is not None else 0.0,
            )
            for r in rows
        ]

    # ---- Introspection ----

    def chunk_count(self) -> int:
        assert self._conn is not None
        return int(self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])

    def entities(self, *, file_id: str | None = None) -> list[dict]:
        assert self._conn is not None
        if file_id is None:
            rows = self._conn.execute(
                "SELECT file_id, label, name, properties, confidence FROM graph_entities"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT file_id, label, name, properties, confidence "
                "FROM graph_entities WHERE file_id = ?",
                (file_id,),
            ).fetchall()
        return [
            {
                "file_id": r["file_id"],
                "label": r["label"],
                "name": r["name"],
                "properties": json.loads(r["properties"] or "{}"),
                "confidence": r["confidence"],
            }
            for r in rows
        ]

    def edges(self, *, file_id: str | None = None) -> list[dict]:
        assert self._conn is not None
        if file_id is None:
            rows = self._conn.execute(
                "SELECT file_id, rel_type, from_name, from_label, to_name, "
                "to_label, confidence FROM graph_edges"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT file_id, rel_type, from_name, from_label, to_name, "
                "to_label, confidence FROM graph_edges WHERE file_id = ?",
                (file_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def neighbors_of_file(self, file_id: str) -> list[dict]:
        """Return entity dicts linked to ``file_id`` via any edge.

        Useful for Phase 6's Q&A to show "this document also mentions
        NUREG-1234 and 10 CFR 50.2" alongside retrieved chunks.
        """
        return self.entities(file_id=file_id)

    # ---- Internals ----

    def _ensure_vec_table(self, dim: int) -> None:
        assert self._conn is not None
        if self._vec_dim is not None and self._vec_dim != dim:
            # Dim changed — drop + recreate. Embedding model mismatch is
            # a bigger problem that should probably blow up, but for now
            # support swap-out during development.
            self._conn.execute("DROP TABLE IF EXISTS chunks_vec")
            self._vec_dim = None
        if self._vec_dim is None:
            self._conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0("
                f"  chunk_id INTEGER PRIMARY KEY, embedding float[{dim}])"
            )
            self._vec_dim = dim


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def _chunk_text(text: str, title: str = "") -> list[str]:
    """Split ``text`` into chunks for indexing using graph-informed semantic chunking.

    Pipeline:
      1. Run the deterministic graph extractor over the full document to
         get structural boundaries enriched by entity/edge signal
         (cross-references, regulatory section markers, persons, headings).
      2. Pass those boundaries to ``axiom.rag.semantic_chunker.chunk_semantic``,
         which respects them when deciding split points.

    Falls back to plain semantic chunking if extraction fails for any reason.

    Day 1 lineage:
      - round 1: 400-char fixed-window shredder (rw-01 missed the answer)
      - round 2: plain semantic chunker (rw-01 surfaced 425°C)
      - round 3: graph-informed semantic chunker (this) — boundaries enriched
        by graph extraction; preserves entity-spanning context across short
        docs and respects regulatory/document references.
    """
    if not text or not text.strip():
        return []

    import os

    from axiom.rag.semantic_chunker import chunk_semantic

    # Escape hatch for benchmarking: AXIOM_CHUNKER_USE_GRAPH=0 disables
    # graph-extractor boundary augmentation, leaving plain semantic chunking.
    # Default (unset or any non-"0" value) → graph-informed.
    use_graph = os.environ.get("AXIOM_CHUNKER_USE_GRAPH", "1") != "0"

    boundaries = None
    if use_graph:
        try:
            from axiom.graph.extractors.deterministic import extract_from_document

            result = extract_from_document(text, title or "doc", "markdown")
            boundaries = result.boundaries
        except Exception:  # noqa: BLE001
            # Extraction must never bring ingestion down — fall back to plain semantic.
            boundaries = None

    chunks = chunk_semantic(text, path=title or "doc", boundaries=boundaries)
    return [c.text for c in chunks]


__all__ = [
    "ClassroomLocalIndex",
    "SearchHit",
]
