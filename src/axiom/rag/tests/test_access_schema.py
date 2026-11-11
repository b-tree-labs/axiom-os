# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for T0-1 access-control schema migration.

Adds three columns to the ``chunks`` and ``documents`` tables:
    access_tier           TEXT (public|course|institutional|classified)
    classification        TEXT (unclassified|cui|sbu|ec|...)
    allowed_nationalities TEXT[] / JSON (NULL = unrestricted)

Tests run against the SQLite store to avoid a live Postgres dependency;
the PG store mirrors the same column set.
"""

from __future__ import annotations

import sqlite3

import pytest

from axiom.rag.chunker import Chunk
from axiom.rag.sqlite_store import SQLiteRAGStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "rag.db"
    s = SQLiteRAGStore(str(db))
    s.connect()
    return s


def _columns(store: SQLiteRAGStore, table: str) -> set[str]:
    conn = store._conn  # noqa: SLF001
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# Schema presence
# ---------------------------------------------------------------------------


class TestAccessSchemaColumns:
    def test_chunks_has_access_tier(self, store):
        assert "access_tier" in _columns(store, "chunks")

    def test_chunks_has_classification(self, store):
        assert "classification" in _columns(store, "chunks")

    def test_chunks_has_allowed_nationalities(self, store):
        assert "allowed_nationalities" in _columns(store, "chunks")

    def test_documents_has_access_tier(self, store):
        assert "access_tier" in _columns(store, "documents")

    def test_documents_has_classification(self, store):
        assert "classification" in _columns(store, "documents")

    def test_documents_has_allowed_nationalities(self, store):
        assert "allowed_nationalities" in _columns(store, "documents")


class TestDefaults:
    def test_new_chunk_defaults_to_public_unclassified(self, store):
        ch = Chunk(
            source_path="x.md",
            source_title="X",
            source_type="markdown",
            text="hello world",
            chunk_index=0,
            start_line=1,
        )
        store.upsert_chunks([ch])
        row = store._conn.execute(  # noqa: SLF001
            "SELECT access_tier, classification, allowed_nationalities "
            "FROM chunks WHERE source_path = 'x.md'"
        ).fetchone()
        assert row[0] == "public"
        assert row[1] == "unclassified"
        # Unrestricted nationalities → NULL
        assert row[2] is None


class TestIdempotentMigration:
    def test_migrate_runs_twice_safely(self, tmp_path):
        db = tmp_path / "rag.db"
        # First connect creates schema.
        s1 = SQLiteRAGStore(str(db))
        s1.connect()
        s1.close()
        # Second connect re-runs migrations; must not raise.
        s2 = SQLiteRAGStore(str(db))
        s2.connect()
        assert "access_tier" in _columns(s2, "chunks")
        s2.close()

    def test_migrate_preserves_existing_row(self, tmp_path):
        """Existing rows get the default values after migration."""
        db = tmp_path / "rag.db"
        # Create a chunks table without the new columns (simulating old install).
        conn = sqlite3.connect(str(db))
        conn.executescript(
            """
            CREATE TABLE chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT NOT NULL,
                source_title TEXT NOT NULL DEFAULT '',
                source_type TEXT NOT NULL DEFAULT 'markdown',
                chunk_text TEXT NOT NULL,
                chunk_index INTEGER NOT NULL DEFAULT 0,
                start_line INTEGER NOT NULL DEFAULT 1,
                corpus TEXT NOT NULL DEFAULT 'rag-internal',
                owner TEXT,
                team TEXT,
                checksum TEXT NOT NULL DEFAULT '',
                chunking_tier TEXT NOT NULL DEFAULT 'fixed',
                corpus_generation INTEGER NOT NULL DEFAULT 1,
                indexed_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT NOT NULL,
                corpus TEXT NOT NULL DEFAULT 'rag-internal',
                source_type TEXT NOT NULL DEFAULT 'markdown',
                title TEXT NOT NULL DEFAULT '',
                checksum TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL DEFAULT '',
                chunk_count INTEGER NOT NULL DEFAULT 0,
                owner TEXT,
                data_source TEXT NOT NULL DEFAULT 'local',
                sync_id TEXT NOT NULL DEFAULT '',
                corpus_generation INTEGER NOT NULL DEFAULT 1,
                graph_extracted_at TEXT,
                first_indexed TEXT NOT NULL DEFAULT '',
                last_indexed TEXT NOT NULL DEFAULT '',
                UNIQUE (source_path, corpus)
            );
            INSERT INTO chunks (source_path, chunk_text) VALUES ('legacy.md', 'old text');
            """
        )
        conn.commit()
        conn.close()
        # Reopen through SQLiteRAGStore — migration should add columns.
        s = SQLiteRAGStore(str(db))
        s.connect()
        row = s._conn.execute(  # noqa: SLF001
            "SELECT access_tier, classification, allowed_nationalities FROM chunks"
        ).fetchone()
        assert row[0] == "public"
        assert row[1] == "unclassified"
        assert row[2] is None
