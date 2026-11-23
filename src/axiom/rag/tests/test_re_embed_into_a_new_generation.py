# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Stored text is re-embedded into a new generation, beside the one being served (#2141).

A corpus whose vectors were made by an earlier embedder setup no longer matches what the
configured embedder makes for the same stored text, so ``embedding-model verify`` refuses to
record a model for it. ``re_embed`` copies the served generation's chunks into a candidate
generation (text, labels and metadata as they are, no vector), embeds them through the guarded
path at each chunk's own label, and keeps the candidate in line with the served generation until
somebody promotes it. Nothing here promotes, and the served generation answers the same
throughout.
"""

from __future__ import annotations

import hashlib
import random
import uuid

import pytest

import axiom.rag.embeddings as emb
from axiom.rag.chunker import Chunk
from axiom.rag.generation import GenerationManager
from axiom.rag.re_embed import ReEmbedRefused, load_exclude_patterns, re_embed

MODEL = "test-embedder"

SHARED = "Shared boilerplate: the reactor operating licence conditions apply to every entry below."


def _vec(text: str) -> list[float]:
    rnd = random.Random(hashlib.sha256(text.encode()).digest())
    return [rnd.uniform(-1.0, 1.0) for _ in range(768)]


def _old_vec(text: str) -> list[float]:
    """What the earlier embedder setup made: another vector for the same text."""
    return _vec("an earlier embedder: " + text)


class Embedder:
    """The configured embedder, at the guard's seam: counts what it is asked for, per label."""

    def __init__(self, *, controlled: bool = False, fail_after: int | None = None) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.controlled = controlled
        self.fail_after = fail_after

    def __call__(self, texts, model, routing_tier):
        if routing_tier == "export_controlled" and not self.controlled:
            return None, None
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise emb.TransientEmbeddingError("the embedder went away", provider="test")
        self.calls.append((routing_tier, list(texts)))
        return [_vec(t) for t in texts], MODEL

    def texts(self) -> list[tuple[str, str]]:
        """Every (label tier, text) embedded for a chunk (the model probe is not one)."""
        return [(tier, t) for tier, batch in self.calls for t in batch if "re-embed probe" not in t]


@pytest.fixture
def embedder(monkeypatch):
    e = Embedder()
    monkeypatch.setattr(emb, "_embed_guarded", e)
    return e


@pytest.fixture
def store(real_postgres):
    from axiom.rag.store import RAGStore

    s = RAGStore(real_postgres.replace("postgresql+psycopg2://", "postgresql://", 1))
    s.connect()
    yield s
    s.close()


@pytest.fixture
def corpus():
    return f"test-re-embed-{uuid.uuid4().hex[:8]}"


def _doc(store, corpus, path, texts, *, title="Report", generation=None, **labels):
    chunks = [
        Chunk(
            text=t,
            source_path=path,
            source_title=title,
            chunk_index=i,
            start_line=1 + 10 * i,
            source_type="markdown",
        )
        for i, t in enumerate(texts)
    ]
    store.upsert_chunks(
        chunks,
        [_old_vec(t) for t in texts],
        corpus=corpus,
        checksum=f"sum-{path}",
        owner="ops",
        corpus_generation=generation,
        **labels,
    )


#: Paths the shared default ignore list drops, and one a site's own exclusion file drops.
JUNK = ".venv/lib/python3.14/site-packages/numpy-2.3.5.dist-info/LICENSE.txt"
CMAKE = "repo/CMakeLists.txt"
SCRATCH = "scratch/tmp-notes.md"


def _seed(store, corpus):
    """Four documents to keep (nine chunks, eight distinct text-and-label pairs) and three to drop."""
    _doc(
        store,
        corpus,
        "ops/log-1.md",
        ["Coolant inlet temperature averaged 38 degrees.", "Rod worth agreed.", SHARED],
    )
    _doc(
        store,
        corpus,
        "ops/log-2.md",
        ["Fuel inspection completed on schedule.", "No reflector degradation.", SHARED],
    )
    _doc(
        store,
        corpus,
        "manuals/triga.md",
        ["The TRIGA manual describes pulsing.", SHARED],
        title="TRIGA Manual",
        access_tier="institutional",
        classification="unclassified",
        allowed_nationalities=["US", "CZ"],
    )
    _doc(
        store,
        corpus,
        "controlled/spec.md",
        ["Controlled fuel specification text."],
        access_tier="export_controlled",
        classification="export_controlled",
    )
    _doc(store, corpus, JUNK, ["numpy licence text.", "more licence text."])
    _doc(store, corpus, CMAKE, ["cmake_minimum_required(VERSION 3.20)"])
    _doc(store, corpus, SCRATCH, ["scratch notes nobody should retrieve."])
    with store._cur() as cur:  # metadata no ingest call sets, to prove the copy keeps every column
        cur.execute(
            "UPDATE chunks SET team = 'reactor-ops', cognitive_type = 'semantic', fragment_ref = 'frag-7' "
            "WHERE corpus = %s AND source_path = 'ops/log-1.md'",
            (corpus,),
        )


KEPT_CHUNKS, KEPT_DOCS, DISTINCT = 9, 4, 8


def _rows(store, corpus, generation, *, identity=False):
    """A generation's chunks as comparable rows: every column but the row id, generation and vector
    (``identity`` adds those, for "exactly as it was")."""
    with store._cur() as cur:
        cur.execute(
            "SELECT * , embedding::text AS embedding_text FROM chunks WHERE corpus = %s AND corpus_generation = %s "
            "ORDER BY source_path, chunk_index, id",
            (corpus, generation),
        )
        rows = [dict(r) for r in cur.fetchall()]
    drop = {"embedding"} if identity else {"id", "corpus_generation", "embedding", "embedding_text"}
    return [{k: v for k, v in r.items() if k not in drop} for r in rows]


def _vectors(store, corpus, generation):
    with store._cur() as cur:
        cur.execute(
            "SELECT source_path, chunk_index, chunk_text, access_tier, embedding::text AS e FROM chunks "
            "WHERE corpus = %s AND corpus_generation = %s ORDER BY source_path, chunk_index",
            (corpus, generation),
        )
        return [dict(r) for r in cur.fetchall()]


def _documents(store, corpus):
    with store._cur() as cur:
        cur.execute("SELECT * FROM documents WHERE corpus = %s ORDER BY source_path", (corpus,))
        return [dict(r) for r in cur.fetchall()]


def _answers(store, corpus, generation=None):
    """What retrieval returns for a fixed set of questions: vector, keyword, both, and by name."""
    q = "coolant inlet temperature"
    qv = _old_vec("Coolant inlet temperature averaged 38 degrees.")
    return {
        "hybrid": store.search(qv, q, corpora=[corpus], limit=10, corpus_generation=generation),
        "vector": store.search(qv, "", corpora=[corpus], limit=10, corpus_generation=generation),
        "keyword": store.search(
            None,
            "licence conditions reactor",
            corpora=[corpus],
            limit=10,
            corpus_generation=generation,
        ),
        "named": store.name_ranking(
            "triga", [corpus], max_sources=5, limit=10, corpus_generation=generation
        ),
    }


def _snapshot(store, corpus, generation=1):
    return {
        "chunks": _rows(store, corpus, generation, identity=True),
        "documents": _documents(store, corpus),
        "answers": _answers(store, corpus),
        "served": store.served_generation(corpus),
    }


EXCLUDE = ["scratch/"]


# ---- the dry run ---------------------------------------------------------------------------------


def test_a_dry_run_reports_the_plan_and_writes_nothing(store, corpus, embedder):
    _seed(store, corpus)
    before = _snapshot(store, corpus)
    with store._cur() as cur:
        cur.execute("SELECT count(*) AS n FROM chunks")
        total = cur.fetchone()["n"]

    out = re_embed(store, corpus, exclude_patterns=EXCLUDE)

    assert out["apply"] is False
    assert (out["served_generation"], out["candidate_generation"]) == (1, 2)
    assert out["model"] == MODEL, "the model it would record"
    assert out["source"] == {"documents": 7, "chunks": 13}
    assert out["excluded"]["paths"] == 3 and out["excluded"]["chunks"] == 4
    assert out["to_copy"] == {"documents": KEPT_DOCS, "chunks": KEPT_CHUNKS}
    assert out["distinct_texts"] == DISTINCT
    assert "dry run" in out["message"]

    assert _snapshot(store, corpus) == before
    with store._cur() as cur:
        cur.execute("SELECT count(*) AS n FROM chunks")
        assert cur.fetchone()["n"] == total
        cur.execute("SELECT to_regclass('rag_re_embed_run') AS t")
        marker = cur.fetchone()["t"]
        if marker:  # another test's apply may have made the table in this shared database
            cur.execute("SELECT count(*) AS n FROM rag_re_embed_run WHERE corpus = %s", (corpus,))
            assert cur.fetchone()["n"] == 0
    assert GenerationManager(store).get_candidate_generation(corpus) is None
    assert store.declared_embedding_models([corpus], corpus_generation=2) == {}


# ---- the build -----------------------------------------------------------------------------------


def test_apply_builds_a_candidate_with_the_same_text_labels_and_metadata(store, corpus, embedder):
    _seed(store, corpus)

    out = re_embed(store, corpus, exclude_patterns=EXCLUDE, apply=True)

    assert out["candidate_generation"] == 2 and out["copied"] == {
        "documents": KEPT_DOCS,
        "chunks": KEPT_CHUNKS,
    }
    assert out["candidate"] == {"documents": KEPT_DOCS, "chunks": KEPT_CHUNKS}
    source = [r for r in _rows(store, corpus, 1) if r["source_path"] not in (JUNK, CMAKE, SCRATCH)]
    assert _rows(store, corpus, 2) == source, "every column of every kept chunk, byte for byte"
    assert {r["team"] for r in source if r["source_path"] == "ops/log-1.md"} == {"reactor-ops"}
    assert {
        tuple(r["allowed_nationalities"] or ())
        for r in source
        if r["source_path"] == "manuals/triga.md"
    } == {("US", "CZ")}


def test_verify_passes_on_the_candidate_and_fails_on_the_old_generation(store, corpus, embedder):
    from axiom.rag.embeddings import verify_embedding_model

    _seed(store, corpus)

    assert verify_embedding_model(store, corpus)["matched"] is False, (
        "the old vectors are why this exists"
    )
    re_embed(store, corpus, exclude_patterns=EXCLUDE, apply=True)

    out = verify_embedding_model(store, corpus, corpus_generation=2)

    assert out["matched"] is True and out["model"] == MODEL
    assert out["sampled"] == 3, (
        "one chunk per document that holds a vector; the controlled one holds none"
    )
    assert out["min_cosine"] >= 0.999


def test_verify_checks_the_vectors_of_a_generation_that_already_records_a_model(
    store, corpus, embedder
):
    """The candidate records its model before it holds a vector, so "a model is recorded" cannot be
    the whole check: vectors that are not that model's are still reported."""
    from axiom.rag.embeddings import verify_embedding_model

    _seed(store, corpus)
    re_embed(store, corpus, exclude_patterns=EXCLUDE, apply=True)
    with store._cur() as cur:
        cur.execute(
            "UPDATE chunks SET embedding = %s WHERE corpus = %s AND corpus_generation = 2 AND embedding IS NOT NULL",
            (str(_old_vec("anything")), corpus),
        )

    out = verify_embedding_model(store, corpus, corpus_generation=2)

    assert out["matched"] is False and out["recorded"] is False
    assert "re-embed" in out["message"]


def test_the_model_is_recorded_before_anything_is_copied(store, corpus, embedder, monkeypatch):
    _seed(store, corpus)
    seen = []
    copy = store.copy_documents_to_generation

    def watched(*args, **kwargs):
        seen.append(store.declared_embedding_models([corpus], corpus_generation=2))
        return copy(*args, **kwargs)

    monkeypatch.setattr(store, "copy_documents_to_generation", watched)

    re_embed(store, corpus, exclude_patterns=EXCLUDE, apply=True)

    assert seen and all(s == {corpus: MODEL} for s in seen)


def test_another_models_vectors_are_refused_by_the_candidate(store, corpus, embedder):
    from axiom.rag.store import EmbeddingModelMismatch

    _seed(store, corpus)
    re_embed(store, corpus, exclude_patterns=EXCLUDE, apply=True)

    with pytest.raises(EmbeddingModelMismatch):
        store.upsert_chunks(
            [Chunk(text="x", source_path="new.md", source_title="N", chunk_index=0, start_line=1)],
            [_vec("x")],
            corpus=corpus,
            corpus_generation=2,
            embedding_model="another-embedder",
        )


def test_identical_texts_are_embedded_once_per_label_and_share_a_vector(store, corpus, embedder):
    _seed(store, corpus)

    out = re_embed(store, corpus, exclude_patterns=EXCLUDE, apply=True)

    asked = [t for _, t in embedder.texts()]
    assert len(asked) == DISTINCT - 1, "the controlled text is never sent"
    assert asked.count(SHARED) == 2, "once for the public label, once for the institutional one"
    assert all(asked.count(t) == 1 for t in asked if t != SHARED)
    assert out["embedded_distinct"] == DISTINCT - 1
    assert out["reused"] == 1, "log-2's copy of the shared paragraph"
    shared = [r for r in _vectors(store, corpus, 2) if r["chunk_text"] == SHARED]
    assert len(shared) == 3 and len({r["e"] for r in shared}) == 1 and shared[0]["e"] is not None


def test_a_controlled_chunk_with_no_admitted_embedder_stays_text_only_and_is_counted(
    store, corpus, embedder
):
    _seed(store, corpus)

    out = re_embed(store, corpus, exclude_patterns=EXCLUDE, apply=True)

    assert out["awaiting"] == 1
    controlled = [r for r in _vectors(store, corpus, 2) if r["access_tier"] == "export_controlled"]
    assert [r["e"] for r in controlled] == [None]
    assert all(tier != "export_controlled" for tier, _ in embedder.calls)
    assert all(
        r["e"] is not None
        for r in _vectors(store, corpus, 2)
        if r["access_tier"] != "export_controlled"
    )


def test_a_later_run_embeds_the_controlled_chunk_once_an_embedder_is_admitted(
    store, corpus, embedder
):
    _seed(store, corpus)
    re_embed(store, corpus, exclude_patterns=EXCLUDE, apply=True)
    embedder.controlled = True

    out = re_embed(store, corpus, exclude_patterns=EXCLUDE, apply=True)

    assert out["copied"]["chunks"] == 0 and out["embedded_distinct"] == 1 and out["awaiting"] == 0


# ---- the served generation is not touched --------------------------------------------------------


def test_the_served_generation_answers_the_same_before_during_and_after(
    store, corpus, embedder, monkeypatch
):
    _seed(store, corpus)
    before = _snapshot(store, corpus)
    assert (
        before["answers"]["hybrid"] and before["answers"]["keyword"] and before["answers"]["named"]
    )
    copy = store.copy_documents_to_generation
    calls = []

    def breaks(*args, **kwargs):
        if len(calls) == 2:
            raise RuntimeError("the connection dropped")
        calls.append(1)
        return copy(*args, **kwargs)

    monkeypatch.setattr(store, "copy_documents_to_generation", breaks)
    with pytest.raises(RuntimeError, match="connection dropped"):
        re_embed(store, corpus, exclude_patterns=EXCLUDE, batch=1, apply=True)

    assert _rows(store, corpus, 2), "the build was under way"
    assert _snapshot(store, corpus) == before, "mid-build"

    monkeypatch.setattr(store, "copy_documents_to_generation", copy)
    out = re_embed(store, corpus, exclude_patterns=EXCLUDE, batch=1, apply=True)

    assert _snapshot(store, corpus) == before, "after the build"
    assert out["promoted"] is False
    assert _answers(store, corpus, 2)["vector"] != before["answers"]["vector"], (
        "the candidate holds new vectors"
    )


def test_the_documents_rows_are_shared_and_never_written(store, corpus, embedder):
    """``documents`` holds one row per (source_path, corpus), whatever the generation: the copy
    writes none, so the served generation's rows are neither moved to the candidate nor changed."""
    _seed(store, corpus)
    before = _documents(store, corpus)

    re_embed(store, corpus, exclude_patterns=EXCLUDE, apply=True)

    assert _documents(store, corpus) == before
    assert {d["corpus_generation"] for d in before} == {1}


def test_it_never_promotes_and_says_what_to_run_next(store, corpus, embedder):
    _seed(store, corpus)

    out = re_embed(store, corpus, exclude_patterns=EXCLUDE, apply=True)

    assert store.served_generation(corpus) == 1
    assert GenerationManager(store).get_candidate_generation(corpus) == 2
    steps = "\n".join(out["next_steps"])
    assert f"embedding-model verify --corpus {corpus} --generation 2" in steps
    assert f"rag promote 2 --corpus {corpus}" in steps
    assert f"rag rollback 1 --corpus {corpus}" in steps


def test_promote_then_rollback_restores_the_old_generation_exactly(store, corpus, embedder):
    _seed(store, corpus)
    before = _snapshot(store, corpus)
    re_embed(store, corpus, exclude_patterns=EXCLUDE, apply=True)
    gm = GenerationManager(store)

    gm.promote(corpus, 2)
    assert store.served_generation(corpus) == 2
    assert _answers(store, corpus) != before["answers"]
    gm.rollback(corpus, 1)

    assert _snapshot(store, corpus) == before


# ---- interrupted, resumed, run again -------------------------------------------------------------


def _state(store, corpus):
    return [
        (r["source_path"], r["chunk_index"], r["chunk_text"], r["access_tier"], r["e"])
        for r in _vectors(store, corpus, 2)
    ]


def test_a_run_interrupted_while_copying_resumes_to_the_same_state(
    store, corpus, embedder, monkeypatch
):
    whole = corpus + "-whole"
    _seed(store, corpus)
    _seed(store, whole)
    re_embed(store, whole, exclude_patterns=EXCLUDE, batch=1, apply=True)
    copy = store.copy_documents_to_generation
    calls = []

    def breaks(*args, **kwargs):
        if len(calls) == 2:
            raise RuntimeError("the connection dropped")
        calls.append(1)
        return copy(*args, **kwargs)

    monkeypatch.setattr(store, "copy_documents_to_generation", breaks)
    with pytest.raises(RuntimeError):
        re_embed(store, corpus, exclude_patterns=EXCLUDE, batch=1, apply=True)
    assert 0 < len(_rows(store, corpus, 2)) < KEPT_CHUNKS
    monkeypatch.setattr(store, "copy_documents_to_generation", copy)

    out = re_embed(store, corpus, exclude_patterns=EXCLUDE, batch=1, apply=True)

    assert out["resumed"] is True
    assert out["copied"]["documents"] == KEPT_DOCS - 2, (
        "what was copied before the failure is not copied again"
    )
    assert _state(store, corpus) == _state(store, whole)
    assert len(_rows(store, corpus, 2)) == KEPT_CHUNKS, "no duplicates"


def test_a_run_interrupted_while_embedding_resumes_to_the_same_state(store, corpus, embedder):
    whole = corpus + "-whole"
    _seed(store, corpus)
    _seed(store, whole)
    re_embed(store, whole, exclude_patterns=EXCLUDE, apply=True, embed_batch=2)
    embedder.calls.clear()
    embedder.fail_after = 3  # the probe, then two batches of two

    with pytest.raises(emb.EmbeddingError):
        re_embed(store, corpus, exclude_patterns=EXCLUDE, apply=True, embed_batch=2)
    embedder.fail_after = None
    done = len(embedder.texts())
    assert 0 < done < DISTINCT - 1

    out = re_embed(store, corpus, exclude_patterns=EXCLUDE, apply=True, embed_batch=2)

    assert out["resumed"] is True and out["copied"]["chunks"] == 0
    assert len(embedder.texts()) == DISTINCT - 1, "nothing is embedded twice"
    assert _state(store, corpus) == _state(store, whole)


def test_a_second_apply_is_a_no_op(store, corpus, embedder):
    _seed(store, corpus)
    re_embed(store, corpus, exclude_patterns=EXCLUDE, apply=True)
    rows = _rows(store, corpus, 2, identity=True)
    embedder.calls.clear()

    out = re_embed(store, corpus, exclude_patterns=EXCLUDE, apply=True)

    assert out["copied"] == {"documents": 0, "chunks": 0} and out["removed"] == {
        "documents": 0,
        "chunks": 0,
    }
    assert out["embedded_distinct"] == 0 and out["reused"] == 0 and out["awaiting"] == 1
    assert embedder.texts() == []
    assert _rows(store, corpus, 2, identity=True) == rows


# ---- catch-up ------------------------------------------------------------------------------------


def test_a_rerun_catches_up_with_what_changed_in_the_served_generation(store, corpus, embedder):
    _seed(store, corpus)
    re_embed(store, corpus, exclude_patterns=EXCLUDE, apply=True)
    untouched = [
        r for r in _rows(store, corpus, 2, identity=True) if r["source_path"] == "manuals/triga.md"
    ]
    embedder.calls.clear()
    # The nightly refresh, writing where retrieval reads: one new, one changed, one deleted, one junk.
    _doc(store, corpus, "ops/log-3.md", ["A new entry for period three."])
    _doc(store, corpus, "ops/log-1.md", ["Coolant inlet temperature averaged 41 degrees.", SHARED])
    with store._cur() as cur:
        cur.execute(
            "DELETE FROM chunks WHERE corpus = %s AND corpus_generation = 1 "
            "AND source_path = 'ops/log-2.md'",
            (corpus,),
        )
        cur.execute(
            "UPDATE chunks SET access_tier = 'course' WHERE corpus = %s AND source_path = 'ops/log-3.md'",
            (corpus,),
        )
    _doc(store, corpus, "pkg/venv/lib/site-packages/requests/README.md", ["requests readme"])

    out = re_embed(store, corpus, exclude_patterns=EXCLUDE, apply=True)

    assert out["copied"] == {"documents": 2, "chunks": 3}
    assert out["removed"]["documents"] == 1, "log-2 is gone from the served generation"
    source = [
        r
        for r in _rows(store, corpus, 1)
        if r["source_path"] not in (JUNK, CMAKE, SCRATCH)
        and "site-packages" not in r["source_path"]
    ]
    assert _rows(store, corpus, 2) == source
    assert {r["source_path"] for r in source} == {
        "ops/log-1.md",
        "ops/log-3.md",
        "manuals/triga.md",
        "controlled/spec.md",
    }
    assert [
        r for r in _rows(store, corpus, 2, identity=True) if r["source_path"] == "manuals/triga.md"
    ] == untouched
    assert sorted(t for _, t in embedder.texts()) == sorted(
        ["A new entry for period three.", "Coolant inlet temperature averaged 41 degrees.", SHARED]
    ), "only what was copied again is embedded again"
    by_text = {(r["source_path"], r["chunk_text"]): r["e"] for r in _vectors(store, corpus, 2)}
    assert by_text[("ops/log-1.md", "Coolant inlet temperature averaged 41 degrees.")] is not None

    again = re_embed(store, corpus, exclude_patterns=EXCLUDE, apply=True)
    assert (
        again["copied"]["chunks"] == 0
        and again["removed"]["chunks"] == 0
        and again["embedded_distinct"] == 0
    )


def test_a_label_change_alone_is_caught_up(store, corpus, embedder):
    _seed(store, corpus)
    re_embed(store, corpus, exclude_patterns=EXCLUDE, apply=True)
    store.set_document_labels(
        "ops/log-2.md", corpus, access_tier="course", classification="unclassified"
    )
    with (
        store._cur() as cur
    ):  # set_document_labels writes every generation; undo the candidate's half
        cur.execute(
            "UPDATE chunks SET access_tier = 'public' WHERE corpus = %s AND corpus_generation = 2 "
            "AND source_path = 'ops/log-2.md'",
            (corpus,),
        )

    out = re_embed(store, corpus, exclude_patterns=EXCLUDE, apply=True)

    assert out["copied"] == {"documents": 1, "chunks": 3}
    assert {
        r["access_tier"] for r in _rows(store, corpus, 2) if r["source_path"] == "ops/log-2.md"
    } == {"course"}


# ---- exclusions ----------------------------------------------------------------------------------


def test_excluded_paths_are_not_copied(store, corpus, embedder):
    _seed(store, corpus)

    out = re_embed(store, corpus, exclude_patterns=EXCLUDE, apply=True)

    paths = {r["source_path"] for r in _rows(store, corpus, 2)}
    assert paths == {"ops/log-1.md", "ops/log-2.md", "manuals/triga.md", "controlled/spec.md"}
    assert out["excluded"]["paths"] == 3 and out["excluded"]["chunks"] == 4
    assert out["excluded"]["by_default"] == 2 and out["excluded"]["by_file"] == 1
    assert not any(
        "licence text" in t or "cmake" in t or "scratch" in t for _, t in embedder.texts()
    )


def test_the_default_ignore_list_applies_without_an_exclusion_file(store, corpus, embedder):
    _seed(store, corpus)

    out = re_embed(store, corpus, apply=True)

    paths = {r["source_path"] for r in _rows(store, corpus, 2)}
    assert JUNK not in paths and CMAKE not in paths and SCRATCH in paths
    assert out["excluded"]["paths"] == 2


def test_an_exclusion_file_is_one_pattern_per_line(tmp_path):
    f = tmp_path / "exclude.txt"
    f.write_text(
        "# junk a connector indexed wholesale\nscratch/\n\n  *.bak  \n*/drafts/*\n",
        encoding="utf-8",
    )

    assert load_exclude_patterns(f) == ["scratch/", "*.bak", "*/drafts/*"]


def test_a_new_exclusion_on_a_rerun_removes_what_was_copied(store, corpus, embedder):
    _seed(store, corpus)
    re_embed(store, corpus, apply=True)

    out = re_embed(store, corpus, exclude_patterns=EXCLUDE, apply=True)

    assert out["removed"] == {"documents": 1, "chunks": 1}
    assert SCRATCH not in {r["source_path"] for r in _rows(store, corpus, 2)}


# ---- refusals ------------------------------------------------------------------------------------


def test_a_candidate_this_command_did_not_start_is_refused_by_name(store, corpus, embedder):
    _seed(store, corpus)
    gm = GenerationManager(store)
    gm.get_active_generation(corpus)
    assert gm.create_candidate(corpus) == 2  # an `axi rag upgrade` under evaluation
    before = _snapshot(store, corpus)

    with pytest.raises(ReEmbedRefused, match="candidate generation 2"):
        re_embed(store, corpus, apply=True)

    assert _snapshot(store, corpus) == before and _rows(store, corpus, 2) == []
    assert "did not start" in re_embed(store, corpus)["refused"], "the dry run says so too"


def test_chunks_left_in_the_next_generation_by_another_build_are_refused(store, corpus, embedder):
    """A discarded build leaves its chunks behind; copying beside them would serve their vectors."""
    _seed(store, corpus)
    _doc(store, corpus, "ops/log-1.md", ["left behind by a build that was discarded"], generation=2)

    with pytest.raises(ReEmbedRefused, match="generation 2 already holds 1 chunk"):
        re_embed(store, corpus, apply=True)


def test_no_embedder_is_refused_before_anything_is_written(store, corpus, monkeypatch):
    _seed(store, corpus)
    monkeypatch.setattr(emb, "_embed_guarded", lambda texts, model, tier: (None, None))

    with pytest.raises(ReEmbedRefused, match="no embedder"):
        re_embed(store, corpus, apply=True)

    assert (
        GenerationManager(store).get_candidate_generation(corpus) is None
        and _rows(store, corpus, 2) == []
    )


def test_a_generation_this_command_built_and_now_serves_is_not_built_again(store, corpus, embedder):
    _seed(store, corpus)
    re_embed(store, corpus, apply=True)
    GenerationManager(store).promote(corpus, 2)

    with pytest.raises(ReEmbedRefused, match="already serves generation 2"):
        re_embed(store, corpus, apply=True)

    assert _rows(store, corpus, 3) == []


def test_after_a_rollback_a_rerun_catches_the_same_candidate_up(store, corpus, embedder):
    _seed(store, corpus)
    re_embed(store, corpus, apply=True)
    gm = GenerationManager(store)
    gm.promote(corpus, 2)
    gm.rollback(corpus, 1)
    _doc(store, corpus, "ops/log-3.md", ["A new entry for period three."])

    out = re_embed(store, corpus, apply=True)

    assert out["candidate_generation"] == 2 and out["copied"] == {"documents": 1, "chunks": 1}
    assert gm.get_candidate_generation(corpus) == 2


def test_the_sqlite_store_is_refused_with_a_reason(tmp_path):
    from axiom.rag.sqlite_store import SQLiteRAGStore

    s = SQLiteRAGStore(f"sqlite:///{tmp_path}/rag.db")
    s.connect()
    try:
        with pytest.raises(ReEmbedRefused, match="one generation"):
            re_embed(s, "rag-internal")
    finally:
        s.close()
