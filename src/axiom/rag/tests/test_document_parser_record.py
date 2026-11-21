# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A document keeps the record of what parsed it (``documents.parser``).

The record is a JSON object (JSONB on Postgres, JSON text on SQLite) naming the
extractor(s) and their versions. Like ``source_url``, a re-index that passes no
record keeps the one an earlier ingest wrote.
"""

from __future__ import annotations

import contextlib

from axiom.rag.chunker import Chunk
from axiom.rag.sqlite_store import SQLiteRAGStore

RECORD = {
    "engine": "site.extract.widget_sheet",
    "version": "1.0.0",
    "extractors": [{"name": "site.extract.widget_sheet", "version": "1.0.0"}],
}


def _chunk(path: str = "specs/a.widget") -> Chunk:
    return Chunk(
        text="body",
        source_path=path,
        source_title="A",
        chunk_index=0,
        start_line=1,
        source_type="widget",
    )


def _sqlite(tmp_path) -> SQLiteRAGStore:
    s = SQLiteRAGStore(f"sqlite:///{tmp_path}/rag.db")
    s.connect()
    return s


def test_sqlite_keeps_the_parser_record(tmp_path):
    s = _sqlite(tmp_path)
    s.upsert_chunks([_chunk()], parser=RECORD)
    assert s.get_document("specs/a.widget")["parser"] == RECORD


def test_sqlite_reindex_without_a_record_keeps_the_old_one(tmp_path):
    s = _sqlite(tmp_path)
    s.upsert_chunks([_chunk()], parser=RECORD)
    s.upsert_chunks([_chunk()])
    assert s.get_document("specs/a.widget")["parser"] == RECORD


def test_sqlite_no_record_reads_as_none(tmp_path):
    s = _sqlite(tmp_path)
    s.upsert_chunks([_chunk()])
    assert s.get_document("specs/a.widget")["parser"] is None


def test_postgres_ddl_adds_the_column():
    from axiom.rag import store

    assert "ALTER TABLE documents ADD COLUMN IF NOT EXISTS parser JSONB;" in " ".join(
        store._SCHEMA_SQL.split()
    )


def test_postgres_upsert_writes_the_record_and_coalesces_it():
    import psycopg2.extras

    from axiom.rag.store import RAGStore

    executed: list[tuple[str, tuple]] = []

    class _Cur:
        def execute(self, sql, params=None):
            executed.append((sql, params or ()))

        def fetchone(self):  # the stored-label lookup (#1971): a new document
            return None

    s = RAGStore("postgresql://unused")

    @contextlib.contextmanager
    def _cur():
        yield _Cur()

    s._cur = _cur  # type: ignore[method-assign]
    s.upsert_chunks([_chunk()], parser=RECORD)

    sql, params = next((q, p) for q, p in executed if "INSERT INTO documents" in q)
    assert "parser" in sql
    assert "COALESCE(EXCLUDED.parser, documents.parser)" in " ".join(sql.split())
    (written,) = [p for p in params if isinstance(p, psycopg2.extras.Json)]
    assert written.adapted == RECORD
