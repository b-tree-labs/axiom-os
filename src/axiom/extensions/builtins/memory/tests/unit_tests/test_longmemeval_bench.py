# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Smoke tests for the LongMemEval benchmark harness (bench-1; 0.17.1).

Tests the runner pipeline against an in-memory synthetic corpus. The
real LongMemEval-S dataset (500 questions from HuggingFace) is exercised
by ``docs/working/run_longmemeval.py`` and its dated JSON outputs — not
unit-tested here (network + cost).
"""

from __future__ import annotations

import pytest


def test_module_imports():
    from axiom.memory.maturation.bench import (
        BenchmarkResult,
        LongMemEvalRunner,
        QuestionResult,
        SyntheticCorpus,
        score_answer,
    )

    assert BenchmarkResult is not None
    assert LongMemEvalRunner is not None
    assert QuestionResult is not None
    assert SyntheticCorpus is not None
    assert score_answer is not None


def test_synthetic_corpus_has_five_questions():
    from axiom.memory.maturation.bench import SyntheticCorpus

    corpus = SyntheticCorpus.small()
    assert len(corpus) == 5
    for q in corpus:
        assert q.question
        assert q.answer
        assert q.haystack_sessions  # non-empty


def test_score_answer_recovers_short_answer():
    """Recall-based: ground truth tokens recovered in retrieved text."""
    from axiom.memory.maturation.bench import score_answer

    # Full recovery
    assert score_answer("May 20", "The deadline is May 20.") == 1.0
    # Half recovery
    assert score_answer("May 20", "The deadline is May.") == 0.5
    # No recovery
    assert score_answer("May 20", "The deadline is unknown.") == 0.0


def test_score_answer_handles_abstention():
    from axiom.memory.maturation.bench import score_answer

    # Ground truth "unknown" + empty retrieved = correct abstain
    assert score_answer("unknown", "") == 1.0
    # Ground truth "unknown" + something retrieved = failed to abstain
    assert score_answer("unknown", "Q3 budget is $50k") == 0.0


def test_score_answer_ignores_stopwords():
    from axiom.memory.maturation.bench import score_answer

    # "the" and "is" are stopwords; "deadline" + "may 20" carry meaning
    s = score_answer("the deadline is May 20", "May 20")
    # truth content tokens after stopwords: {deadline, may, 20}; retrieved: {may, 20}
    # recall = 2/3
    assert s == pytest.approx(2 / 3, abs=0.01)


def test_runner_baseline_finds_synthetic_answers():
    """The baseline retrieval-only configuration should still find most answers
    in the synthetic corpus (it's easy: relevant turns are in the haystack).
    """
    from axiom.memory.maturation.bench import LongMemEvalRunner, SyntheticCorpus

    corpus = SyntheticCorpus.small()
    runner = LongMemEvalRunner(configuration="baseline")
    result = runner.run(corpus, corpus_name="synthetic-test")

    assert result.n_questions == 5
    assert result.configuration == "baseline"
    # Synthetic corpus is easy — expect ≥ 4/5 correct.
    assert result.n_correct >= 4
    assert result.accuracy >= 0.8


def test_runner_matured_doesnt_regress_synthetic():
    """The matured pipeline must not regress baseline on the synthetic corpus.

    On real LongMemEval-S (multi-session reasoning) we expect matured >
    baseline; on the easy synthetic corpus we only assert no regression.
    """
    from axiom.memory.maturation.bench import LongMemEvalRunner, SyntheticCorpus

    corpus = SyntheticCorpus.small()
    baseline = LongMemEvalRunner(configuration="baseline").run(corpus)
    matured = LongMemEvalRunner(configuration="matured").run(corpus)

    assert matured.accuracy >= baseline.accuracy


def test_benchmark_result_to_dict_round_trip():
    from axiom.memory.maturation.bench import LongMemEvalRunner, SyntheticCorpus

    corpus = SyntheticCorpus.small()
    result = LongMemEvalRunner(configuration="baseline").run(corpus)
    d = result.to_dict()
    assert d["n_questions"] == 5
    assert d["configuration"] == "baseline"
    assert "accuracy" in d
    assert "mean_f1" in d
    assert "per_question" in d
    assert len(d["per_question"]) == 5


def test_runner_rejects_invalid_configuration():
    from axiom.memory.maturation.bench import LongMemEvalRunner

    with pytest.raises(ValueError, match="configuration must be"):
        LongMemEvalRunner(configuration="wat")
