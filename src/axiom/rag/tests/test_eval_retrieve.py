# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi rag eval`` measures the real retriever.

The eval used to retrieve with ``store.search(query_text=...)``, which with no
query embedding is full-text only: no vector search, no fusion, no rerank. So
turning the rerank on or off gave identical numbers. These acceptance tests pin
the retrieval-only mode to :func:`axiom.rag.retriever.retrieve`, the path chat
uses, and its rank-based scoring.

Acceptance rows: EVAL-R1 retrieve() not store.search; EVAL-R2 rerank on/off
differ; EVAL-R3 recall@k and MRR; EVAL-R4 old question files and answer mode;
EVAL-R5 a custom scorer through the seam.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from axiom.rag.store import SearchResult


def _hit(path: str, text: str, *, title: str = "", chunk: int = 0) -> SearchResult:
    return SearchResult(
        source_path=path,
        source_title=title or path.rsplit("/", 1)[-1],
        chunk_text=text,
        chunk_index=chunk,
        similarity=0.5,
        combined_score=0.5,
        corpus="rag-internal",
    )


# A fixture the rerank reorders: both backends rank the generic page first,
# but only the specific page carries the query's terms, so the heuristic rerank
# (coverage) lifts it to rank 1.
_GENERIC = _hit("docs/intro.md", "A general introduction to this handbook.")
_SPECIFIC = _hit("docs/lattice.md", "The lattice spacing measurement procedure.")


class _RankingStore:
    """Answers every backend call (vector or text) with the same ranking."""

    def __init__(self, hits):
        self.hits = hits
        self.calls: list[dict] = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return list(self.hits)

    def close(self):
        pass


class _NoDirectSearchStore:
    def search(self, **kwargs):
        raise AssertionError("eval must retrieve through retrieve(), not store.search")

    def close(self):
        pass


def _questions(tmp_path, body: str):
    f = tmp_path / "q.yaml"
    f.write_text(body)
    return f


_LATTICE_Q = (
    "- id: q1\n"
    "  question: 'lattice spacing measurement'\n"
    "  expected_sources: ['lattice.md']\n"
)


def _no_model():
    raise AssertionError("retrieval-only eval must not build a model")


# -- EVAL-R1 -----------------------------------------------------------------


def test_eval_r1_retrieval_mode_calls_retrieve_not_store_search(tmp_path, capsys):
    """EVAL-R1: each question goes through retrieve() with the chat embedding."""
    from axiom.rag import cli as rag_cli
    from axiom.rag.retriever import RetrievedChunk

    calls: list[dict] = []

    def spy_retrieve(store, query_text, query_embedding, **kwargs):
        calls.append({"query": query_text, "embedding": query_embedding, **kwargs})
        return [RetrievedChunk(
            citation_key="C1", rank=1, source_path="docs/lattice.md",
            source_title="lattice", chunk_text="x", chunk_index=0,
            corpus="rag-internal", similarity=0.5, rrf_score=0.1,
        )]

    path = _questions(tmp_path, _LATTICE_Q)
    with patch("axiom.rag.cli._get_store", return_value=_NoDirectSearchStore()), \
         patch("axiom.rag.cli._build_model_fn", side_effect=_no_model), \
         patch("axiom.rag.embeddings.embed_texts", return_value=[[0.1, 0.2]]), \
         patch("axiom.rag.retriever.retrieve", side_effect=spy_retrieve):
        rag_cli.main(["eval", "--questions", str(path), "--retrieval-only",
                      "--k", "3", "--rerank", "off"])

    assert len(calls) == 1
    assert calls[0]["query"] == "lattice spacing measurement"
    assert calls[0]["embedding"] == [0.1, 0.2]   # vector half of the hybrid
    assert calls[0]["limit"] == 3
    assert calls[0]["rerank"] is False
    out = capsys.readouterr().out
    assert "q1" in out and "rank 1" in out


def test_eval_r1_rerank_omitted_uses_the_retriever_default(tmp_path):
    """EVAL-R1: no --rerank flag passes nothing, so retrieve()'s default governs."""
    from axiom.rag import cli as rag_cli

    calls: list[dict] = []

    def spy_retrieve(store, query_text, query_embedding, **kwargs):
        calls.append(kwargs)
        return []

    path = _questions(tmp_path, _LATTICE_Q)
    with patch("axiom.rag.cli._get_store", return_value=_NoDirectSearchStore()), \
         patch("axiom.rag.embeddings.embed_texts", return_value=None), \
         patch("axiom.rag.retriever.retrieve", side_effect=spy_retrieve):
        rag_cli.main(["eval", "--questions", str(path), "--retrieval-only"])

    assert "rerank" not in calls[0]


# -- EVAL-R2 -----------------------------------------------------------------


def _run_retrieval_cli(tmp_path, capsys, *extra):
    from axiom.rag import cli as rag_cli

    path = _questions(tmp_path, _LATTICE_Q)
    store = _RankingStore([_GENERIC, _SPECIFIC])
    with patch("axiom.rag.cli._get_store", return_value=store), \
         patch("axiom.rag.cli._build_model_fn", side_effect=_no_model), \
         patch("axiom.rag.embeddings.embed_texts", return_value=[[0.1, 0.2]]):
        rag_cli.main(["eval", "--questions", str(path), "--retrieval-only", *extra])
    return store, capsys.readouterr().out


def test_eval_r2_rerank_on_and_off_rank_differently(tmp_path, capsys):
    """EVAL-R2: through the real retrieve(), the rerank changes the rank."""
    store, out_on = _run_retrieval_cli(tmp_path, capsys, "--rerank", "on")
    # Hybrid: both a vector and a text backend call, not text-only.
    assert any(c.get("query_embedding") is not None for c in store.calls)
    assert any(c.get("query_text") for c in store.calls)
    _, out_off = _run_retrieval_cli(tmp_path, capsys, "--rerank", "off")

    assert "rank 1" in out_on
    assert "rank 2" in out_off
    assert "mrr: 1.000" in out_on.lower()
    assert "mrr: 0.500" in out_off.lower()


def test_eval_r2_compare_runs_both_configurations_in_one_run(tmp_path, capsys):
    """EVAL-R2: --rerank both reports each configuration and the per-question change."""
    _, out = _run_retrieval_cli(tmp_path, capsys, "--rerank", "both")
    low = out.lower()
    assert "rerank on" in low and "rerank off" in low
    assert "q1: rank 1 (rerank on) vs rank 2 (rerank off)" in low


# -- EVAL-R3 -----------------------------------------------------------------


def test_eval_r3_recall_at_k_and_mrr_on_a_hand_checked_fixture():
    """EVAL-R3: recall@k and MRR, checked by hand.

    q1 suffix path match at rank 2 (1/2); q2 title match at rank 1 (1);
    q3 missed (0); q4 named chunk 3 of alpha, found at rank 3 (1/3);
    q5 names nothing and is not scored.
    recall@3 = 3/4 = 0.75, MRR = (0.5 + 1 + 0 + 1/3) / 4 = 0.458333...
    """
    from axiom.rag.eval import Citation, RagEvalQuestion, run_retrieval_eval

    def c(path, *, title="", chunk=0):
        return Citation(source_path=path, source_title=title, chunk_index=chunk)

    ranked = {
        "one": [c("docs/other.md"), c("Docs/Guide/Alpha.md")],
        "two": [c("x/beta.pdf", title="Beta Manual"), c("docs/other.md")],
        "three": [c("docs/other.md")],
        "four": [c("docs/guide/alpha.md", chunk=1), c("docs/other.md"),
                 c("docs/guide/alpha.md", chunk=3)],
        "five": [c("docs/other.md")],
    }
    qs = [
        RagEvalQuestion(id="q1", question="one", expected_sources=["guide/alpha.md"]),
        RagEvalQuestion(id="q2", question="two", expected_sources=["beta manual"]),
        RagEvalQuestion(id="q3", question="three", expected_sources=["gamma.md"]),
        RagEvalQuestion(id="q4", question="four", expected_sources=["alpha.md"],
                        expected_chunks=[3]),
        RagEvalQuestion(id="q5", question="five"),
    ]
    report = run_retrieval_eval(qs, retriever_fn=lambda q: ranked[q], k=3)

    by_id = {r.question_id: r for r in report.runs}
    assert by_id["q1"].rank == 2
    assert by_id["q2"].rank == 1
    assert by_id["q3"].rank is None and "missed" in by_id["q3"].note
    assert by_id["q4"].rank == 3
    assert by_id["q5"].scored is False
    assert report.scored == 4
    assert report.recall_at_k == pytest.approx(0.75)
    assert report.mrr == pytest.approx((0.5 + 1 + 0 + 1 / 3) / 4)


def test_eval_r3_partial_segment_is_not_a_path_suffix_match():
    """EVAL-R3: a suffix match is on whole path segments ("pha.md" is not "alpha.md")."""
    from axiom.rag.eval import Citation, RagEvalQuestion, score_first_expected_rank

    q = RagEvalQuestion(id="q", question="x", expected_sources=["pha.md"])
    assert score_first_expected_rank(q, [Citation(source_path="a/alpha.md")]).rank is None


# -- EVAL-R4 -----------------------------------------------------------------


_OLD_FILE = (
    "- id: q1\n"
    "  question: 'Which material was named?'\n"
    "  expected_answer_contains: ['widget', 'gasket']\n"
    "  expected_citations: ['parts.pdf']\n"
    "  tags: ['inventory']\n"
)


def test_eval_r4_old_questions_file_loads_and_answer_mode_scores_as_before(tmp_path):
    """EVAL-R4: an old-format file loads and answer scoring is unchanged."""
    from axiom.rag.eval import Citation, load_questions, run_eval

    qs = load_questions(_questions(tmp_path, _OLD_FILE))
    assert qs[0].expected_answer_contains == ["widget", "gasket"]
    assert qs[0].expected_citations == ["parts.pdf"]
    assert qs[0].expected_sources == [] and qs[0].expected_chunks == []

    report = run_eval(
        qs,
        model_fn=lambda prompt, context=None: "It was the widget.",
        retriever_fn=lambda q: [Citation(source_path="parts.pdf"),
                                Citation(source_path="other.pdf")],
    )
    assert report.runs[0].answer_score == 0.5      # 1 of 2 phrases
    assert report.runs[0].citation_score == 0.5    # Jaccard {parts} / {parts, other}
    assert report.passed == 0


def test_eval_r4_new_fields_load_and_old_citations_feed_rank_scoring(tmp_path):
    """EVAL-R4: the new fields load; an old file's expected_citations still score ranks."""
    from axiom.rag.eval import Citation, load_questions, score_first_expected_rank

    qs = load_questions(_questions(tmp_path, (
        "- id: q1\n"
        "  question: 'x'\n"
        "  expected_sources: ['a.md', 'Some Title']\n"
        "  expected_chunks: [2]\n"
        "  expected_pages: [7]\n"
    )))
    assert qs[0].expected_sources == ["a.md", "Some Title"]
    assert qs[0].expected_chunks == [2]
    assert qs[0].expected_pages == [7]

    old = load_questions(_questions(tmp_path, _OLD_FILE))[0]
    hits = [Citation(source_path="x.pdf"), Citation(source_path="dir/parts.pdf")]
    assert score_first_expected_rank(old, hits).rank == 2


def test_eval_r4_answer_mode_retrieves_through_retrieve(tmp_path, capsys):
    """EVAL-R4: the answer mode keeps its scoring but now gets its context from retrieve()."""
    from axiom.rag import cli as rag_cli

    seen: list[str | None] = []

    def fake_model(prompt, *, context=None):
        seen.append(context)
        return "the widget" if context and "widget" in context else "unknown"

    store = _RankingStore([_hit("parts.pdf", "The widget and the gasket.")])
    path = _questions(tmp_path, _OLD_FILE)
    with patch("axiom.rag.cli._get_store", return_value=store), \
         patch("axiom.rag.cli._build_model_fn", return_value=fake_model), \
         patch("axiom.rag.embeddings.embed_texts", return_value=[[0.1]]):
        rag_cli.main(["eval", "--questions", str(path)])

    out = capsys.readouterr().out.lower()
    assert "lift (with - baseline) on mean answer score: +0.500" in out
    assert any(c.get("query_embedding") is not None for c in store.calls)


# -- EVAL-R5 -----------------------------------------------------------------


def test_eval_r5_custom_scorer_plugged_through_the_seam(tmp_path, capsys):
    """EVAL-R5: a scorer registered in eval.RANK_SCORERS is selectable without CLI changes."""
    from axiom.rag import cli as rag_cli
    from axiom.rag import eval as rag_eval

    seen: list[tuple[str, int]] = []

    def always_second(question, ranked):
        seen.append((question.id, len(ranked)))
        return rag_eval.RankScore(rank=2, score=0.5, note="custom says second")

    path = _questions(tmp_path, _LATTICE_Q)
    store = _RankingStore([_GENERIC, _SPECIFIC])
    with patch.dict(rag_eval.RANK_SCORERS, {"always-second": always_second}), \
         patch("axiom.rag.cli._get_store", return_value=store), \
         patch("axiom.rag.embeddings.embed_texts", return_value=None):
        rag_cli.main(["eval", "--questions", str(path), "--retrieval-only",
                      "--scorer", "always-second"])

    out = capsys.readouterr().out.lower()
    assert seen == [("q1", 2)]
    assert "custom says second" in out
    assert "mrr: 0.500" in out


def test_eval_r5_library_runner_takes_a_scorer():
    """EVAL-R5: run_retrieval_eval uses the scorer it is given."""
    from axiom.rag.eval import Citation, RagEvalQuestion, RankScore, run_retrieval_eval

    def scorer(q, ranked):
        return RankScore(rank=None, score=0.0, note=f"saw {len(ranked)}")

    report = run_retrieval_eval(
        [RagEvalQuestion(id="q", question="x", expected_sources=["a"])],
        retriever_fn=lambda q: [Citation(source_path="a")], k=1, scorer=scorer,
    )
    assert report.runs[0].note == "saw 1"
    assert report.recall_at_k == 0.0
