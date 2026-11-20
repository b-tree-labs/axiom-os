# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""ADR-091: a document's shareable origin link round-trips through the RAG store.

The ingest embedder threads ``FetchedItem.source_url`` + ``item_id`` into
``store.upsert_chunks(source_url=…, source_ref_id=…)`` (landed source-side in
#531). This locks in the store half: the columns persist and read back, a
URL-less re-index does NOT wipe a link a prior ingest captured (COALESCE on
conflict), and URL-less kinds leave the columns NULL rather than fabricating.

No Postgres needed — exercised against the SQLite store, which mirrors the
Postgres DDL/upsert.
"""

from __future__ import annotations

from axiom.rag.chunker import Chunk
from axiom.rag.sqlite_store import SQLiteRAGStore

BOX_URL = "https://app.box.com/file/123"


def _store(tmp_path) -> SQLiteRAGStore:
    s = SQLiteRAGStore(f"sqlite:///{tmp_path}/rag.db")
    s.connect()
    return s


def _chunk(path: str = "reports/q1.pdf") -> Chunk:
    return Chunk(
        text="body paragraph",
        source_path=path,
        source_title="Q1",
        chunk_index=0,
        start_line=1,
        source_type="pdf",
    )


def test_upsert_persists_source_url_and_ref_id(tmp_path):
    s = _store(tmp_path)
    s.upsert_chunks([_chunk()], source_url=BOX_URL, source_ref_id="123")

    doc = s.get_document("reports/q1.pdf")
    assert doc is not None
    assert doc["source_url"] == BOX_URL
    assert doc["source_ref_id"] == "123"


def test_url_less_reindex_does_not_wipe_existing_link(tmp_path):
    """A local re-embed carries no URL; COALESCE-on-conflict keeps the link."""
    s = _store(tmp_path)
    s.upsert_chunks([_chunk()], source_url=BOX_URL, source_ref_id="123")

    # Re-index the same path with NO url/ref (the failure mode the store fix guards).
    s.upsert_chunks([_chunk()])

    doc = s.get_document("reports/q1.pdf")
    assert doc["source_url"] == BOX_URL
    assert doc["source_ref_id"] == "123"


def test_url_less_kind_leaves_columns_null(tmp_path):
    """URL-less kinds (local FS) are exempt by declaration — NULL, not fabricated."""
    s = _store(tmp_path)
    s.upsert_chunks([_chunk("local/notes.md")])

    doc = s.get_document("local/notes.md")
    assert doc is not None
    assert doc["source_url"] is None
    assert doc["source_ref_id"] is None
