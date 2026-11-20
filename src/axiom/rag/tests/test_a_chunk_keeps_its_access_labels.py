# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A chunk keeps the access labels it was ingested with, in both stores.

``chunks`` and ``documents`` have ``access_tier``, ``classification`` and ``allowed_nationalities`` columns, and
``retrieve`` filters on them, but ``upsert_chunks`` never wrote them: every chunk read as public and unclassified,
whatever its source. A tier the retrieval filter does not know is refused at write time, so a typo cannot store a
label that silently hides (or exposes) a document.
"""

from __future__ import annotations

import json
import uuid

import pytest

from axiom.rag.chunker import Chunk
from axiom.rag.retriever import AccessContext, retrieve
from axiom.rag.sqlite_store import SQLiteRAGStore

TEXT = ("The shield plate sits between the inner housing and the outer casing. Its thickness is the difference "
        "between the two radii shown in the figure, 1.333 m and 1.206 m, which is 0.127 m.")


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path):
    if request.param == "sqlite":
        s = SQLiteRAGStore(f"sqlite:///{tmp_path}/rag.db")
    else:
        from axiom.rag.store import RAGStore

        s = RAGStore(request.getfixturevalue("real_postgres").replace("postgresql+psycopg2://", "postgresql://", 1))
    s.connect()
    yield s
    s.close()


@pytest.fixture
def corpus():
    return f"test-labels-{uuid.uuid4().hex[:8]}"


def _chunk(path: str) -> Chunk:
    return Chunk(text=TEXT, source_path=path, source_title="Report", chunk_index=0, start_line=1, source_type="pdf")


def _document_labels(store, path: str, corpus: str) -> tuple:
    sql = "SELECT access_tier, classification, allowed_nationalities FROM documents WHERE source_path = {0} AND corpus = {0}"
    if isinstance(store, SQLiteRAGStore):
        tier, cls, nats = store._conn.execute(sql.format("?"), (path, corpus)).fetchone()
        return tier, cls, tuple(json.loads(nats)) if nats else None
    with store._cur() as cur:
        cur.execute(sql.format("%s"), (path, corpus))
        row = cur.fetchone()
    return row["access_tier"], row["classification"], tuple(row["allowed_nationalities"] or ()) or None


def _search_labels(store, corpus: str) -> list[tuple]:
    hits = store.search(query_text="shield plate", corpora=[corpus], limit=5)
    return sorted((h.source_path, h.access_tier, h.classification, h.allowed_nationalities) for h in hits)


def test_labels_reach_the_chunks_and_the_document(store, corpus):
    store.upsert_chunks([_chunk("a.pdf")], corpus=corpus, access_tier="institutional", classification="cui",
                        allowed_nationalities=("US",))
    assert _search_labels(store, corpus) == [("a.pdf", "institutional", "cui", ("US",))]
    assert _document_labels(store, "a.pdf", corpus) == ("institutional", "cui", ("US",))


def test_without_labels_a_chunk_reads_as_it_always_has(store, corpus):
    store.upsert_chunks([_chunk("a.pdf")], corpus=corpus)
    assert _search_labels(store, corpus) == [("a.pdf", "public", "unclassified", None)]
    assert _document_labels(store, "a.pdf", corpus) == ("public", "unclassified", None)


def test_re_ingesting_with_new_labels_replaces_them(store, corpus):
    store.upsert_chunks([_chunk("a.pdf")], corpus=corpus, access_tier="institutional")
    store.upsert_chunks([_chunk("a.pdf")], corpus=corpus, access_tier="course")
    assert _search_labels(store, corpus) == [("a.pdf", "course", "unclassified", None)]
    assert _document_labels(store, "a.pdf", corpus)[0] == "course"


@pytest.mark.parametrize("tier", ["export-controlled", "Public", "secret"])
def test_an_unknown_tier_is_refused_and_nothing_is_written(store, corpus, tier):
    with pytest.raises(ValueError, match="access_tier"):
        store.upsert_chunks([_chunk("a.pdf")], corpus=corpus, access_tier=tier)
    assert _search_labels(store, corpus) == []


def test_retrieval_filters_on_the_stored_labels(store, corpus):
    store.upsert_chunks([_chunk("open.pdf")], corpus=corpus)
    store.upsert_chunks([_chunk("closed.pdf")], corpus=corpus, access_tier="institutional")

    def paths(ctx):
        return {c.source_path for c in retrieve(store, "shield plate", None, corpora=[corpus], access_context=ctx)}

    assert paths(AccessContext()) == {"open.pdf"}
    assert paths(AccessContext(max_access_tier="institutional")) == {"open.pdf", "closed.pdf"}


def test_an_export_controlled_chunk_is_hidden_from_every_reader_today(store, corpus):
    """Stored faithfully, and fail-closed: the retrieval filter has no export-controlled reader yet."""
    store.upsert_chunks([_chunk("ec.pdf")], corpus=corpus, access_tier="export_controlled")
    assert _search_labels(store, corpus) == [("ec.pdf", "export_controlled", "unclassified", None)]
    for ctx in (AccessContext(), AccessContext(max_access_tier="classified")):
        assert retrieve(store, "shield plate", None, corpora=[corpus], access_context=ctx) == []


def test_a_re_index_that_declares_nothing_keeps_the_labels(store, corpus):
    """A label set by a person or the backfill survives a re-index that declares none (#1971)."""
    store.upsert_chunks([_chunk("a.pdf")], corpus=corpus, access_tier="institutional", classification="cui")
    store.upsert_chunks([_chunk("a.pdf")], corpus=corpus)
    assert _search_labels(store, corpus) == [("a.pdf", "institutional", "cui", None)]
    assert _document_labels(store, "a.pdf", corpus) == ("institutional", "cui", None)
