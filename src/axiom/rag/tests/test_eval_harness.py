# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RAG eval harness — Qwen-with-RAG vs Qwen-baseline (and any model).

Built 2026-06-01 per Ben's lakehouse goal: 'Qwen LLM benchmarked with
and without RAG.' This module is the value-proof harness, not the
ingest/index code. It runs a curated question set against a model
callable + an optional retriever callable, and produces a structured
report with per-question outcomes + aggregate scores.

Scoring is deliberately simple for v0:
- substring match on ``expected_answer_contains`` → answerability
- citation overlap with ``expected_citations`` → citation faithfulness
- latency per call → operational signal

The harness is model-agnostic — Qwen, Claude, a stub for tests, any
``Callable[[prompt], str]`` works. Same for retrievers
(``Callable[[query], list[Citation]]``).

Follow-up: tighter scoring (LLM-judge for faithfulness, ROUGE for
content, etc.) lands as v1 once the v0 harness has produced enough
runs to establish baselines.
"""

from __future__ import annotations

from axiom.rag.eval import (
    Citation,
    RagEvalQuestion,
    run_eval,
    score_citation_overlap,
    score_substring,
)

# -- scoring primitives -------------------------------------------------------


class TestSubstringScore:
    def test_all_required_phrases_present_returns_one(self):
        assert score_substring("The CP2 reactor used graphite moderator.",
                               ["CP2", "graphite"]) == 1.0

    def test_partial_match_returns_fraction(self):
        # 1 of 2 phrases present
        assert score_substring("Only mentions CP2.",
                               ["CP2", "graphite"]) == 0.5

    def test_empty_required_returns_one(self):
        assert score_substring("anything", []) == 1.0

    def test_case_insensitive(self):
        assert score_substring("the cp2 REACTOR ran on Graphite",
                               ["CP2", "graphite"]) == 1.0


class TestCitationOverlap:
    def test_perfect_overlap_returns_one(self):
        got = [Citation(source_path="a.pdf"), Citation(source_path="b.pdf")]
        exp = ["a.pdf", "b.pdf"]
        assert score_citation_overlap(got, exp) == 1.0

    def test_partial_overlap_returns_jaccard(self):
        got = [Citation(source_path="a.pdf")]
        exp = ["a.pdf", "b.pdf"]
        # Jaccard: |{a}| / |{a, b}| = 0.5
        assert score_citation_overlap(got, exp) == 0.5

    def test_no_overlap_returns_zero(self):
        got = [Citation(source_path="c.pdf")]
        exp = ["a.pdf", "b.pdf"]
        assert score_citation_overlap(got, exp) == 0.0

    def test_no_expected_citations_returns_one(self):
        """If the question doesn't require citations, don't penalize."""
        assert score_citation_overlap([Citation("anything.pdf")], []) == 1.0


# -- end-to-end run + report --------------------------------------------------


def test_run_eval_baseline_no_retrieval():
    """Model-only path: no retriever; citations always empty."""
    questions = [
        RagEvalQuestion(id="q1", question="What is CP2?",
                        expected_answer_contains=["graphite"]),
    ]
    def model(prompt, context=None):
        return "CP2 was a graphite-moderated reactor."

    report = run_eval(questions, model_fn=model, retriever_fn=None)

    assert len(report.runs) == 1
    r = report.runs[0]
    assert r.question_id == "q1"
    assert r.retrieval_enabled is False
    assert r.answer.startswith("CP2 was")
    assert r.answer_score == 1.0
    assert r.citation_score == 1.0  # no required citations


def test_run_eval_with_retrieval_calls_retriever_and_passes_context():
    captured = {}
    questions = [
        RagEvalQuestion(id="q1", question="What moderator did CP2 use?",
                        expected_answer_contains=["graphite"],
                        expected_citations=["cp2-details.pdf"]),
    ]

    def retriever(query):
        captured["query"] = query
        return [Citation(source_path="cp2-details.pdf",
                         chunk_text="CP2 graphite moderator details ...")]

    def model(prompt, context=None):
        captured["context"] = context
        return "CP2 used a graphite moderator."

    report = run_eval(questions, model_fn=model, retriever_fn=retriever)

    assert captured["query"] == "What moderator did CP2 use?"
    assert "graphite" in captured["context"]
    r = report.runs[0]
    assert r.retrieval_enabled is True
    assert r.answer_score == 1.0
    assert r.citation_score == 1.0
    assert len(r.citations) == 1


def test_report_aggregate_scores_average_over_runs():
    questions = [
        RagEvalQuestion(id="q1", question="?", expected_answer_contains=["foo"]),
        RagEvalQuestion(id="q2", question="?", expected_answer_contains=["bar"]),
    ]
    # one correct, one wrong
    def model(prompt, context=None):
        return "this mentions foo only"
    report = run_eval(questions, model_fn=model, retriever_fn=None)

    assert report.mean_answer_score == 0.5
    assert report.total == 2
    assert report.passed == 1


def test_run_eval_captures_latency_ms():
    questions = [RagEvalQuestion(id="q1", question="?")]
    def model(prompt, context=None):
        return "x"
    report = run_eval(questions, model_fn=model, retriever_fn=None)
    assert report.runs[0].latency_ms >= 0


def test_compare_helper_diff_with_vs_without_retrieval():
    """The headline shape Ben asked for: 'Qwen with vs without RAG.'"""
    from axiom.rag.eval import compare_with_and_without_retrieval

    questions = [
        RagEvalQuestion(id="q1", question="What moderator did CP2 use?",
                        expected_answer_contains=["graphite"]),
    ]

    def retriever(query):
        return [Citation(source_path="cp2.pdf", chunk_text="graphite moderator")]

    # baseline doesn't know; with-context does
    def model(prompt, context=None):
        if context and "graphite" in context:
            return "Graphite."
        return "I don't know."

    diff = compare_with_and_without_retrieval(
        questions, model_fn=model, retriever_fn=retriever,
    )
    assert diff.baseline.mean_answer_score == 0.0
    assert diff.with_retrieval.mean_answer_score == 1.0
    assert diff.lift == 1.0   # delta in mean_answer_score


# -- IO ---------------------------------------------------------------------


def test_load_questions_from_yaml(tmp_path):
    from axiom.rag.eval import load_questions

    f = tmp_path / "questions.yaml"
    f.write_text(
        "- id: q1\n"
        "  question: 'What is CP2?'\n"
        "  expected_answer_contains: ['graphite', 'moderator']\n"
        "  expected_citations: ['cp2.pdf']\n"
        "- id: q2\n"
        "  question: 'Foo?'\n"
        "  expected_answer_contains: ['bar']\n"
    )

    qs = load_questions(f)
    assert len(qs) == 2
    assert qs[0].id == "q1"
    assert qs[0].expected_citations == ["cp2.pdf"]
    assert qs[1].expected_citations == []


# --- v1 instrument: notation normalization + abstention scoring -----------

def test_normalized_substring_matches_unicode_subscripts():
    from axiom.rag.eval import score_substring_normalized
    # "BeF₂" answer vs "BeF2" key — was a false negative on nuclear-v0.
    assert score_substring_normalized("flush salt was LiF-BeF₂", ["BeF2"]) == 1.0
    assert score_substring_normalized("U²³⁵ cross section", ["U235"]) == 1.0


def test_abstention_scorer_rewards_decline_and_punishes_hallucination():
    from axiom.rag.eval import score_abstention
    assert score_abstention("That is not in the corpus.") == 1.0
    assert score_abstention("I could not find any documents on that.") == 1.0
    # A confident answer to an absence question = hallucination = fail.
    assert score_abstention("The value is 42 MW.") == 0.0


def test_load_questions_parses_expected_behavior(tmp_path):
    from axiom.rag.eval import load_questions
    p = tmp_path / "q.yaml"
    p.write_text(
        "- id: a1\n"
        "  question: 'made-up fact not in corpus?'\n"
        "  expected_behavior: abstain\n"
        "  review_status: unreviewed\n"
        "  tags: ['adversarial', 'absence']\n"
    )
    qs = load_questions(p)
    assert qs[0].expected_behavior == "abstain"
    assert qs[0].review_status == "unreviewed"
    assert "absence" in qs[0].tags
