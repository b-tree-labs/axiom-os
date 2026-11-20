# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A corpus declares which model made its vectors, once, and holds writes and queries to it (P0.2).

``embed_texts`` takes the first backend that answers (a remote server, OpenAI or Ollama), and nothing
recorded which one made a vector. Two documents of one corpus could be embedded by different models,
whose vectors live in different spaces: a query embedded by one model then ranks the other's documents
by noise, with no error anywhere.

The first vectors written to a corpus generation declare its model, in one row per generation
(``rag_corpus_embedding_model``); nothing scans the corpus. A write from another model is refused
before anything is written. A query embedded by another model is not used: retrieval falls back to
keyword search and says why. A new generation may declare a new model, which is how a corpus moves
to one.
"""

from __future__ import annotations

import uuid

import pytest

import axiom.rag.embeddings as emb
from axiom.rag.chunker import Chunk
from axiom.rag.sqlite_store import SQLiteRAGStore
from axiom.rag.store import EmbeddingModelMismatch

VEC = [0.01] * 768
TEXT = "The thermal shield is 0.127 m thick, the difference between the radii 1.333 m and 1.206 m in the figure."


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
    return f"test-model-{uuid.uuid4().hex[:8]}"


def _chunk(path: str) -> Chunk:
    return Chunk(text=TEXT, source_path=path, source_title="Report", chunk_index=0, start_line=1, source_type="pdf")


def _paths(store, corpus: str) -> set[str]:
    sql = "SELECT source_path FROM chunks WHERE corpus = {0}"
    if isinstance(store, SQLiteRAGStore):
        return {r[0] for r in store._conn.execute(sql.format("?"), (corpus,)).fetchall()}
    with store._cur() as cur:
        cur.execute(sql.format("%s"), (corpus,))
        return {r["source_path"] for r in cur.fetchall()}


def test_the_first_vectors_declare_the_corpus_model(store, corpus):
    store.upsert_chunks([_chunk("a.pdf")], [VEC], corpus=corpus, embedding_model="nomic-embed-text")
    assert store.declared_embedding_models([corpus]) == {corpus: "nomic-embed-text"}


def test_a_second_model_in_one_corpus_is_refused(store, corpus):
    store.upsert_chunks([_chunk("a.pdf")], [VEC], corpus=corpus, embedding_model="nomic-embed-text")
    with pytest.raises(EmbeddingModelMismatch, match="nomic-embed-text"):
        store.upsert_chunks([_chunk("b.pdf")], [VEC], corpus=corpus, embedding_model="text-embedding-3-small")
    assert _paths(store, corpus) == {"a.pdf"}  # b.pdf was not written


def test_the_declaration_holds_even_for_the_document_that_made_it(store, corpus):
    """Declared once per corpus, not inferred from what is stored: re-embedding the only document with
    another model is still a second model in the corpus."""
    store.upsert_chunks([_chunk("a.pdf")], [VEC], corpus=corpus, embedding_model="nomic-embed-text")
    with pytest.raises(EmbeddingModelMismatch):
        store.upsert_chunks([_chunk("a.pdf")], [VEC], corpus=corpus, embedding_model="text-embedding-3-small")


def test_another_corpus_may_use_another_model(store, corpus):
    store.upsert_chunks([_chunk("a.pdf")], [VEC], corpus=corpus, embedding_model="nomic-embed-text")
    store.upsert_chunks([_chunk("a.pdf")], [VEC], corpus=corpus + "-b", embedding_model="text-embedding-3-small")
    assert store.declared_embedding_models([corpus + "-b"]) == {corpus + "-b": "text-embedding-3-small"}


def test_a_new_generation_may_declare_a_new_model(store, corpus):
    if isinstance(store, SQLiteRAGStore):
        pytest.skip("the SQLite store serves one generation")
    store.upsert_chunks([_chunk("a.pdf")], [VEC], corpus=corpus, embedding_model="nomic-embed-text", corpus_generation=1)
    store.upsert_chunks(
        [_chunk("a.pdf")], [VEC], corpus=corpus, embedding_model="text-embedding-3-small", corpus_generation=2
    )
    assert store.declared_embedding_models([corpus], corpus_generation=2) == {corpus: "text-embedding-3-small"}
    assert store.declared_embedding_models([corpus]) == {corpus: "nomic-embed-text"}  # generation 1 is served


def test_a_caller_that_names_no_model_is_accepted_as_before(store, corpus):
    store.upsert_chunks([_chunk("a.pdf")], [VEC], corpus=corpus)
    store.upsert_chunks([_chunk("b.pdf")], [VEC], corpus=corpus, embedding_model="nomic-embed-text")
    assert _paths(store, corpus) == {"a.pdf", "b.pdf"}
    assert store.declared_embedding_models([corpus]) == {corpus: "nomic-embed-text"}


def test_text_only_chunks_declare_nothing(store, corpus):
    store.upsert_chunks([_chunk("a.pdf")], None, corpus=corpus, embedding_model="nomic-embed-text")
    assert store.declared_embedding_models([corpus]) == {}


# ---- the query side ----------------------------------------------------------------------------


def _embedder(model: str, monkeypatch) -> None:
    monkeypatch.setattr(emb, "embed_texts_with_model", lambda texts, *a, **k: ([VEC for _ in texts], model))


def test_a_query_from_the_declared_model_is_used(store, corpus, monkeypatch):
    store.upsert_chunks([_chunk("a.pdf")], [VEC], corpus=corpus, embedding_model="nomic-embed-text")
    _embedder("nomic-embed-text", monkeypatch)

    q = emb.embed_query("thermal shield", store=store, corpora=[corpus])

    assert q.vector == VEC and q.note == ""


def test_a_query_from_another_model_falls_back_to_keyword_with_a_note(store, corpus, monkeypatch):
    store.upsert_chunks([_chunk("a.pdf")], [VEC], corpus=corpus, embedding_model="nomic-embed-text")
    _embedder("text-embedding-3-small", monkeypatch)

    q = emb.embed_query("thermal shield", store=store, corpora=[corpus])

    assert q.vector is None
    assert "keyword" in q.note.lower() and "nomic-embed-text" in q.note


def test_writers_declare_the_model_that_made_their_vectors(store, corpus, monkeypatch, tmp_path):
    """``axi rag index`` (``ingest_file``) records the model the platform chain answered with."""
    from axiom.rag import ingest as ingest_mod

    monkeypatch.setattr(emb, "_embed_remote", lambda texts: None)
    monkeypatch.setattr(emb, "_embed_openai", lambda texts, model: None)
    monkeypatch.setattr(emb, "_embed_ollama", lambda texts: [VEC for _ in texts])
    doc = tmp_path / "report.md"
    doc.write_text("# Report\n\n" + TEXT + "\n", encoding="utf-8")

    ingest_mod.ingest_file(doc, store, corpus=corpus, repo_root=tmp_path)

    assert store.declared_embedding_models([corpus]) == {corpus: emb._OLLAMA_EMBED_MODEL}


def test_a_connector_pull_declares_the_model_and_reports_a_second_one(store, corpus, monkeypatch, tmp_path):
    """The connector path (``embed_bronze_record``) declares its model, and a document a second model
    embedded is reported as not indexed rather than failing the pull."""
    from axiom.extensions.builtins.data_platform import rag_embed as re_mod
    from axiom.extensions.builtins.data_platform.rag_embed.tests.test_embed_bronze_record import _build_bronze

    monkeypatch.setattr(emb, "_embed_remote", lambda texts: None)
    monkeypatch.setattr(emb, "_embed_openai", lambda texts, model: None)
    monkeypatch.setattr(emb, "_embed_ollama", lambda texts: [VEC for _ in texts])
    record, item = _build_bronze(tmp_path, content=b"# Report\n\n" + TEXT.encode() + b"\n", source_path="/r/a.md")
    record = record.__class__(**{**record.__dict__, "tier": corpus})

    assert re_mod.embed_bronze_record(record, item, store).indexed is True
    assert store.declared_embedding_models([corpus]) == {corpus: emb._OLLAMA_EMBED_MODEL}

    monkeypatch.setattr(emb, "_OLLAMA_EMBED_MODEL", "another-embedder")
    stats = re_mod.embed_bronze_record(record, item, store)

    assert stats.indexed is False
    assert (stats.skipped_reason or "").startswith("embedding_model_mismatch")


# ---- mixed models: only the mismatched corpus falls back (owner's decision 3) -----------------


@pytest.fixture
def pg(real_postgres):
    from axiom.rag.store import RAGStore

    s = RAGStore(real_postgres.replace("postgresql+psycopg2://", "postgresql://", 1))
    s.connect()
    yield s
    s.close()


def _two_corpora(store, corpus):
    """``good`` declared the query's model, ``stale`` another. Neither chunk shares a word with the query,
    so only a vector search can find either."""
    good, stale = corpus + "-good", corpus + "-stale"
    store.upsert_chunks([Chunk(text="alpha bravo charlie", source_path="g.md", source_title="G", chunk_index=0,
                               start_line=1)], [VEC], corpus=good, embedding_model="nomic-embed-text")
    store.upsert_chunks([Chunk(text="delta echo foxtrot", source_path="s.md", source_title="S", chunk_index=0,
                               start_line=1)], [VEC], corpus=stale, embedding_model="text-embedding-3-small")
    store.upsert_chunks([Chunk(text="thermal shield note", source_path="k.md", source_title="K", chunk_index=0,
                               start_line=1)], None, corpus=stale)
    return good, stale


def test_a_mixed_query_falls_back_only_for_the_mismatched_corpus(pg, corpus, monkeypatch):
    from axiom.rag.retriever import UNFILTERED, retrieve

    good, stale = _two_corpora(pg, corpus)
    _embedder("nomic-embed-text", monkeypatch)

    q = emb.embed_query("thermal shield", store=pg, corpora=[good, stale])

    assert q.vector == VEC, "the corpus that matches keeps vector search"
    assert q.keyword_only == (stale,)
    assert stale in q.note and good not in q.note

    hits = retrieve(pg, "thermal shield", q.vector, corpora=[good, stale], limit=10,
                    access_context=UNFILTERED, keyword_only_corpora=q.keyword_only)
    paths = {h.source_path for h in hits}

    assert "g.md" in paths, "vector hits from the corpus that matches"
    assert "k.md" in paths, "keyword hits from the corpus that fell back"
    assert "s.md" not in paths, "no vector ranking of the mismatched corpus"


def test_a_direct_search_falls_back_only_for_the_mismatched_corpus(pg, corpus, monkeypatch):
    """A path that calls ``store.search`` itself (library search; the chat surfaces go through ``retrieve()``)."""
    from axiom.rag.retriever import search_with_fallback

    good, stale = _two_corpora(pg, corpus)
    _embedder("nomic-embed-text", monkeypatch)
    q = emb.embed_query("thermal shield", store=pg, corpora=[good, stale])

    paths = {h.source_path for h in search_with_fallback(pg, q, "thermal shield", corpora=[good, stale], limit=10)}

    assert paths == {"g.md", "k.md"}


def test_when_every_searched_corpus_mismatches_the_query_has_no_vector(pg, corpus, monkeypatch):
    good, stale = _two_corpora(pg, corpus)
    _embedder("another-model", monkeypatch)

    q = emb.embed_query("thermal shield", store=pg, corpora=[good, stale])

    assert q.vector is None and set(q.keyword_only) == {good, stale}


# ---- old corpora: verify, then record (owner's decision 2) -------------------------------------


def _legacy(store, corpus, n_docs=3, vec=VEC):
    """Vectors written before records existed: no model named, so nothing is recorded."""
    for d in range(n_docs):
        store.upsert_chunks(
            [Chunk(text=f"document {d} passage", source_path=f"d{d}.md", source_title="D", chunk_index=0,
                   start_line=1)],
            [vec], corpus=corpus,
        )


def test_an_old_corpus_has_no_record_and_doctor_says_run_verify(pg, corpus):
    from axiom.rag.ingest import embedding_doctor_rows

    _legacy(pg, corpus)

    assert pg.declared_embedding_models([corpus]) == {}
    rows = [r for r in embedding_doctor_rows(pg) if corpus in r["name"]]
    assert any("embedding model not recorded; run verify" in r["status"] for r in rows), rows


def test_verify_is_a_dry_run_by_default(pg, corpus, monkeypatch):
    from axiom.rag.embeddings import verify_embedding_model

    _legacy(pg, corpus)
    _embedder("nomic-embed-text", monkeypatch)

    out = verify_embedding_model(pg, corpus)

    assert out["matched"] is True and out["recorded"] is False and out["sampled"] == 3
    assert pg.declared_embedding_models([corpus]) == {}


def test_verify_records_the_model_with_apply_when_every_sample_matches(pg, corpus, monkeypatch):
    from axiom.rag.embeddings import verify_embedding_model

    _legacy(pg, corpus)
    _embedder("nomic-embed-text", monkeypatch)

    out = verify_embedding_model(pg, corpus, apply=True)

    assert out["recorded"] is True
    assert pg.declared_embedding_models([corpus]) == {corpus: "nomic-embed-text"}


def test_verify_refuses_to_record_when_a_sample_does_not_match(pg, corpus, monkeypatch):
    """Stored vectors from another model: refused, reported, nothing recorded; the corpus needs a re-embed."""
    from axiom.rag.embeddings import verify_embedding_model

    other = [0.0] * 767 + [1.0]
    _legacy(pg, corpus, vec=other)
    _embedder("nomic-embed-text", monkeypatch)

    out = verify_embedding_model(pg, corpus, apply=True)

    assert out["matched"] is False and out["recorded"] is False
    assert out["min_cosine"] < 0.999
    assert "re-embed" in out["message"]
    assert pg.declared_embedding_models([corpus]) == {}


def test_verify_samples_across_documents(pg, corpus, monkeypatch):
    from axiom.rag.embeddings import verify_embedding_model

    _legacy(pg, corpus, n_docs=30)
    _embedder("nomic-embed-text", monkeypatch)

    out = verify_embedding_model(pg, corpus)

    assert out["sampled"] == 20 and out["documents"] == 20


def test_the_verify_skill_declares_a_reversible_write():
    from axiom.extensions.builtins.data_platform.skills import _CHAINING, _SKILLS

    assert "embedding_model_verify" in _SKILLS
    assert _CHAINING["embedding_model_verify"]["risk"] == "reversible-write"


def test_the_catch_up_honours_the_recorded_model(pg, corpus, monkeypatch):
    """A corpus that recorded one model never gets vectors from another through the catch-up."""
    from axiom.rag.ingest import embed_awaiting

    pg.upsert_chunks([_chunk("a.pdf")], [VEC], corpus=corpus, embedding_model="text-embedding-3-small")
    pg.upsert_chunks([_chunk("b.pdf")], None, corpus=corpus)
    monkeypatch.setattr(emb, "_embed_remote", lambda texts: None)
    monkeypatch.setattr(emb, "_embed_openai", lambda texts, model: None)
    monkeypatch.setattr(emb, "_embed_ollama", lambda texts: [VEC for _ in texts])  # nomic-embed-text

    assert embed_awaiting(pg, corpus) == {"embedded": 0, "waiting": 1}


def test_the_cli_spelling_reaches_the_verify_skill():
    """``axi data embedding-model verify --corpus C`` dispatches to ``data.embedding_model_verify``."""
    from axiom.extensions.builtins.data_platform.commands.cli import _args_to_params, _parser, resolve_skill

    args = _parser().parse_args(["embedding-model", "verify", "--corpus", "c1", "--apply"])

    assert resolve_skill(args) == ("embedding_model_verify", None)
    params = _args_to_params(args)
    assert params["corpus"] == "c1" and params["apply"] is True and params["sample"] == 20
