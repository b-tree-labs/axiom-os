# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Retrieval serves each corpus's active generation; a candidate is invisible until promoted (#1945).

Blue/green rebuilds (``axi rag upgrade`` / ``promote`` / ``rollback``) write a candidate
generation next to the active one. Before this, ``RAGStore.search`` accepted
``corpus_generation`` and never filtered on it, ``retrieve()`` never passed one, and
``upsert_chunks`` never wrote it, so a candidate's chunks were served beside the active
ones and writing a candidate deleted the active chunks of the same path.

Now:

- ``upsert_chunks`` writes the generation it is given, or the corpus's served
  generation when it is given none, and replaces chunks only within that generation;
- ``search`` reads each corpus's served generation (``rag_generation_config``, 1
  when a corpus has no row) unless the caller names one;
- ``retrieve(corpus_generation=N)`` targets a generation explicitly, for evaluation.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock

import pytest

from axiom.rag.chunker import Chunk
from axiom.rag.retriever import UNFILTERED, retrieve
from axiom.rag.store import RAGStore, SearchResult

# -- SQL shape (mock cursor, no database) -------------------------------------


def _mock_store(capture: list, *, has_config: bool = True, active: int | None = None):
    store = RAGStore.__new__(RAGStore)  # bypass __init__/DB
    store._vector_type = "vector"
    store._has_generation_config = has_config
    cur = MagicMock()
    cur.fetchall.return_value = []
    # One row shape answers both lookups upsert makes: the served generation and the stored labels.
    cur.fetchone.return_value = (
        {
            "active_generation": active,
            "access_tier": "public",
            "classification": "unclassified",
            "allowed_nationalities": None,
        }
        if active is not None
        else None
    )

    def _execute(sql, params=None):
        capture.append((sql, params))

    cur.execute.side_effect = _execute

    @contextlib.contextmanager
    def _cur():
        yield cur

    store._cur = _cur
    return store


def _search_sql(capture: list) -> str:
    return next(sql for sql, _ in capture if "FROM chunks" in sql and "SELECT" in sql)


@pytest.mark.parametrize(
    ("embedding", "text"),
    [
        ([0.1] * 4, ""),  # vector only
        (None, "annual revenue"),  # text only
        ([0.1] * 4, "annual revenue"),  # hybrid
        ([0.1] * 4, "status of unit XR7 annual revenue region"),  # hybrid with an identifier
    ],
)
def test_every_search_branch_reads_the_served_generation(embedding, text):
    cap: list = []
    _mock_store(cap).search(query_embedding=embedding, query_text=text)
    sql = _search_sql(cap)
    assert "rag_generation_config" in sql
    # Every scan of chunks that can contribute a row carries the predicate.
    scans = sql.count("WHERE corpus = ANY(%s)")
    assert scans >= 1
    assert sql.count("corpus_generation = COALESCE(") == scans


def test_a_named_generation_is_read_instead_of_the_served_one():
    cap: list = []
    _mock_store(cap).search(query_embedding=[0.1] * 4, query_text="", corpus_generation=2)
    sql = _search_sql(cap)
    assert "corpus_generation = 2" in sql
    assert "rag_generation_config" not in sql


def test_a_store_without_the_config_table_serves_generation_one():
    cap: list = []
    _mock_store(cap, has_config=False).search(query_embedding=None, query_text="annual revenue")
    sql = _search_sql(cap)
    assert "corpus_generation = 1" in sql
    assert "rag_generation_config" not in sql


def _chunk(path: str = "notes/a.md", text: str = "body") -> Chunk:
    return Chunk(text=text, source_path=path, source_title="A", chunk_index=0, start_line=1)


def test_upsert_writes_the_named_generation_and_replaces_only_within_it():
    cap: list = []
    _mock_store(cap).upsert_chunks([_chunk()], corpus="rag-org", corpus_generation=3)
    delete = next((sql, p) for sql, p in cap if sql.lstrip().startswith("DELETE FROM chunks"))
    assert "corpus_generation = %s" in delete[0]
    assert delete[1][-1] == 3
    insert = next((sql, p) for sql, p in cap if "INSERT INTO chunks" in sql)
    assert "corpus_generation" in insert[0]
    assert 3 in insert[1]
    doc = next((sql, p) for sql, p in cap if "INSERT INTO documents" in sql)
    assert "corpus_generation" in doc[0]
    assert 3 in doc[1]


def test_upsert_without_a_generation_writes_the_served_one():
    """Ordinary ingest must land where retrieval reads, not in a hard-coded generation 1."""
    cap: list = []
    _mock_store(cap, active=2).upsert_chunks([_chunk()], corpus="rag-org")
    insert = next((sql, p) for sql, p in cap if "INSERT INTO chunks" in sql)
    assert 2 in insert[1]


# -- retrieve() passes the generation through ---------------------------------


class _RecordingStore:
    def __init__(self):
        self.calls: list[dict] = []

    def search(self, **kw):
        self.calls.append(kw)
        return [SearchResult("a.md", "A", "body", 0, 0.9, 0.9, corpus="rag-org")]


def test_retrieve_targets_a_named_generation():
    s = _RecordingStore()
    retrieve(
        s, "body", [0.1] * 4, corpora=["rag-org"], access_context=UNFILTERED, corpus_generation=2
    )
    assert s.calls and all(c.get("corpus_generation") == 2 for c in s.calls)


def test_retrieve_without_a_generation_leaves_the_store_to_serve_the_active_one():
    s = _RecordingStore()
    retrieve(s, "body", [0.1] * 4, corpora=["rag-org"], access_context=UNFILTERED)
    # Not passed at all, so a store whose search() predates the keyword still works.
    assert s.calls and all("corpus_generation" not in c for c in s.calls)


# -- a real Postgres: the issue's acceptance ----------------------------------


@pytest.fixture
def pg_store(fresh_postgres):
    store = RAGStore(fresh_postgres.replace("postgresql+psycopg2://", "postgresql://", 1))
    try:
        store.connect()
    except Exception as exc:  # noqa: BLE001 - no pgvector on this server
        pytest.skip(f"RAG store needs pgvector: {exc}")
    yield store
    store.close()


def _write(
    store: RAGStore, path: str, text: str, generation: int | None, corpus: str = "rag-org"
) -> None:
    store.upsert_chunks(
        [_chunk(path, text)], [[0.01] * 768], corpus=corpus, corpus_generation=generation
    )


def _paths(store: RAGStore, **kw) -> set[str]:
    hits = retrieve(
        store, "turbine", [0.01] * 768, corpora=["rag-org"], access_context=UNFILTERED, **kw
    )
    return {h.source_path for h in hits}


def test_a_candidate_is_invisible_until_promoted(pg_store):
    from axiom.rag.generation import GenerationManager

    gm = GenerationManager(pg_store)
    assert gm.get_active_generation("rag-org") == 1
    _write(
        pg_store, "docs/old.md", "turbine maintenance, old build", None
    )  # lands in the active generation
    candidate = gm.create_candidate("rag-org")
    assert candidate == 2
    _write(pg_store, "docs/new.md", "turbine maintenance, new build", candidate)

    assert _paths(pg_store) == {"docs/old.md"}
    # The evaluation path can read the candidate before it serves.
    assert _paths(pg_store, corpus_generation=candidate) == {"docs/new.md"}

    gm.promote("rag-org", candidate)
    assert _paths(pg_store) == {"docs/new.md"}

    gm.rollback("rag-org", 1)
    assert _paths(pg_store) == {"docs/old.md"}


def test_writing_a_candidate_keeps_the_active_chunks_of_the_same_path(pg_store):
    from axiom.rag.generation import GenerationManager

    gm = GenerationManager(pg_store)
    _write(pg_store, "docs/same.md", "turbine maintenance, active text", None)
    candidate = gm.create_candidate("rag-org")
    _write(pg_store, "docs/same.md", "turbine maintenance, candidate text", candidate)

    hits = retrieve(
        pg_store, "turbine", [0.01] * 768, corpora=["rag-org"], access_context=UNFILTERED
    )
    assert [h.chunk_text for h in hits] == ["turbine maintenance, active text"]


def test_ordinary_ingest_lands_in_a_promoted_generation(pg_store):
    from axiom.rag.generation import GenerationManager

    gm = GenerationManager(pg_store)
    gm.promote("rag-org", gm.create_candidate("rag-org"))  # active is now 2
    _write(pg_store, "docs/later.md", "turbine maintenance, added after the promote", None)
    assert _paths(pg_store) == {"docs/later.md"}


def test_the_generation_report_counts_what_retrieval_hides(pg_store, capsys, monkeypatch):
    """``axi rag generations`` is the read-only check before and after a release."""
    from axiom.rag import cli as rag_cli
    from axiom.rag.generation import GenerationManager

    gm = GenerationManager(pg_store)
    _write(pg_store, "docs/a.md", "turbine maintenance, served", None)
    _write(pg_store, "docs/b.md", "turbine maintenance, candidate", gm.create_candidate("rag-org"))

    rows = {r["corpus"]: r for r in pg_store.generation_report()}
    assert rows["rag-org"]["served_generation"] == 1
    assert rows["rag-org"]["candidate_generation"] == 2
    assert rows["rag-org"]["served"] == 1
    assert rows["rag-org"]["hidden"] == 1

    monkeypatch.setattr(
        rag_cli, "_get_store", lambda **_k: pg_store
    )  # the CLI closes it; the fixture's close is a no-op
    rag_cli.cmd_generations(type("A", (), {"json": False})())
    out = capsys.readouterr().out
    assert "rag-org" in out and "1:1, 2:1" in out


def test_name_ranking_and_neighbour_reads_skip_a_hidden_generation(pg_store):
    """#1984's name ranking and neighbour expansion read chunks directly; they read the served
    generation too, so a candidate (or a stale generation) never reaches an answer that way."""
    from axiom.rag.generation import GenerationManager

    gm = GenerationManager(pg_store)
    _write(pg_store, "manuals/pump-guide.md", "pump guide, served text", None)
    candidate = gm.create_candidate("rag-org")
    _write(pg_store, "manuals/pump-guide.md", "pump guide, candidate text", candidate)

    def texts(hits):
        return {h.chunk_text for h in hits}

    named = pg_store.name_ranking("pump & guide", ["rag-org"], max_sources=3, limit=5)
    assert texts(named) == {"pump guide, served text"}
    named = pg_store.name_ranking(
        "pump & guide", ["rag-org"], max_sources=3, limit=5, corpus_generation=candidate
    )
    assert texts(named) == {"pump guide, candidate text"}

    at = pg_store.chunks_at([("manuals/pump-guide.md", "rag-org", 0)], ["rag-org"])
    assert texts(at) == {"pump guide, served text"}
    best = pg_store.best_chunks_in(["manuals/pump-guide.md"], "pump guide", ["rag-org"], limit=5)
    assert texts(best) == {"pump guide, served text"}
