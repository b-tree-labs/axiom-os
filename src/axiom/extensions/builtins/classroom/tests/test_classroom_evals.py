# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the classroom evals framework.

Tier B piece — prove the vector+graph+LLM combo actually helps by
running a bank of questions through the pipeline and scoring them.
First cut is deliberately modest: keyword-based scoring (does the
answer mention the expected terms?), one pipeline (Axiom), no
baseline comparison. Follow-up work layers an LLM-judge and a
no-retrieval baseline to quantify the lift.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.classroom.classroom_evals import (
    BaselineReport,
    ComparisonReport,
    EvalBank,
    EvalQuestion,
    EvalReport,
    EvalResult,
    compare,
    load_bank,
    run_bank,
    run_baseline,
    score_keywords,
)
from axiom.extensions.builtins.classroom.classroom_qna import Citation

# ---------------------------------------------------------------------------
# Question bank format
# ---------------------------------------------------------------------------


class TestBankFormat:
    def test_load_bank_from_jsonl(self, tmp_path):
        path = tmp_path / "bank.jsonl"
        path.write_text(
            '{"question": "What is fission?", "expected_keywords": ["heavy nuclei", "neutron"]}\n'
            '{"question": "What is a control rod?", "expected_keywords": ["absorb", "neutron"]}\n'
        )
        bank = load_bank(path)
        assert isinstance(bank, EvalBank)
        assert len(bank.questions) == 2
        assert bank.questions[0].question == "What is fission?"
        assert bank.questions[0].expected_keywords == ["heavy nuclei", "neutron"]

    def test_load_bank_rejects_missing_fields(self, tmp_path):
        path = tmp_path / "bad.jsonl"
        path.write_text('{"question": "q"}\n')  # missing expected_keywords
        with pytest.raises(ValueError, match="expected_keywords"):
            load_bank(path)

    def test_load_bank_skips_blank_lines(self, tmp_path):
        path = tmp_path / "bank.jsonl"
        path.write_text(
            '\n'
            '{"question": "q1", "expected_keywords": ["k"]}\n'
            '\n'
            '{"question": "q2", "expected_keywords": ["k2"]}\n'
            '\n'
        )
        bank = load_bank(path)
        assert len(bank.questions) == 2


# ---------------------------------------------------------------------------
# Keyword scorer
# ---------------------------------------------------------------------------


class TestKeywordScorer:
    def test_all_keywords_present_scores_full(self):
        result = score_keywords(
            answer="Control rods absorb neutrons to slow fission.",
            expected_keywords=["absorb", "neutron"],
        )
        assert result.passed is True
        assert result.hit_keywords == ["absorb", "neutron"]
        assert result.missed_keywords == []

    def test_missing_keyword_fails(self):
        result = score_keywords(
            answer="The reactor is cooled by water.",
            expected_keywords=["absorb", "neutron"],
        )
        assert result.passed is False
        assert result.missed_keywords == ["absorb", "neutron"]

    def test_case_insensitive(self):
        result = score_keywords(
            answer="ABSORB NEUTRONS.",
            expected_keywords=["absorb", "neutron"],
        )
        assert result.passed is True

    def test_multi_word_keyword(self):
        result = score_keywords(
            answer="The process is called nuclear fission because heavy nuclei split.",
            expected_keywords=["heavy nuclei"],
        )
        assert result.passed is True

    def test_empty_answer_fails(self):
        result = score_keywords(
            answer="",
            expected_keywords=["absorb"],
        )
        assert result.passed is False


# ---------------------------------------------------------------------------
# Bank runner — full path from question → retrieved citations → LLM → score
# ---------------------------------------------------------------------------


def _fake_retriever(question: str, k: int = 3) -> list[Citation]:
    """Returns citations based on keywords in the question — just enough
    to simulate retrieval without building a real index."""
    q = question.lower()
    out: list[Citation] = []
    if "control rod" in q:
        out.append(Citation(
            title="Chapter 2 — Control rods",
            text="Control rods absorb neutrons to slow fission.",
            file_id="f2",
        ))
    if "fission" in q:
        out.append(Citation(
            title="Chapter 1 — Fission",
            text="Fission splits heavy nuclei releasing neutrons and energy.",
            file_id="f1",
        ))
    return out[:k]


def _fake_llm_echo_citations(prompt: str, *, system: str = "") -> str:
    """Pretends to synthesize: returns the concatenated citation texts.

    Good enough to test that the bank runner threads question →
    retrieval → answer → score end-to-end.
    """
    # Simulate a real grounded tutor by returning content from the prompt.
    if "control rod" in prompt.lower():
        return "A control rod absorbs neutrons to slow fission. [Chapter 2]"
    if "fission" in prompt.lower():
        return "Fission splits heavy nuclei, releasing neutrons. [Chapter 1]"
    return "I couldn't find that in your class materials."


class TestRunBank:
    def test_runner_returns_report_with_per_question_results(self):
        bank = EvalBank(questions=[
            EvalQuestion(
                question="What is a control rod?",
                expected_keywords=["absorb", "neutron"],
            ),
            EvalQuestion(
                question="What is fission?",
                expected_keywords=["heavy nuclei", "neutron"],
            ),
        ])
        report = run_bank(
            bank=bank,
            retrieve=_fake_retriever,
            llm=_fake_llm_echo_citations,
        )
        assert isinstance(report, EvalReport)
        assert len(report.results) == 2
        for r in report.results:
            assert isinstance(r, EvalResult)
            assert r.answer
            assert r.score.passed is True

    def test_report_summary_stats_reflect_pass_rate(self):
        bank = EvalBank(questions=[
            EvalQuestion(
                question="What is a control rod?",
                expected_keywords=["absorb", "neutron"],
            ),
            EvalQuestion(
                question="What color is the sky?",  # not in materials
                expected_keywords=["blue"],
            ),
        ])
        report = run_bank(
            bank=bank,
            retrieve=_fake_retriever,
            llm=_fake_llm_echo_citations,
        )
        assert report.total == 2
        assert report.passed == 1
        assert report.failed == 1
        # 50%.
        assert abs(report.pass_rate - 0.5) < 0.0001

    def test_runner_records_citations_for_each_question(self):
        bank = EvalBank(questions=[
            EvalQuestion(
                question="What is a control rod?",
                expected_keywords=["absorb"],
            ),
        ])
        report = run_bank(
            bank=bank,
            retrieve=_fake_retriever,
            llm=_fake_llm_echo_citations,
        )
        assert report.results[0].citations
        assert report.results[0].citations[0].title == "Chapter 2 — Control rods"

    def test_baseline_runs_without_retrieval(self):
        """Baseline path calls the LLM directly with just the question,
        no citations, no retrieval — isolates what the LLM alone can do.
        """
        def baseline_llm(prompt: str, *, system: str = "") -> str:
            # Pretend to know the world broadly — gives the right answer
            # for well-known NE concepts.
            p = prompt.lower()
            if "control rod" in p:
                return "A control rod absorbs neutrons in a reactor."
            return "I don't know."

        bank = EvalBank(questions=[
            EvalQuestion(
                question="What is a control rod?",
                expected_keywords=["absorb", "neutron"],
            ),
            EvalQuestion(
                question="What is XYZZY-42?",  # made up
                expected_keywords=["flux"],
            ),
        ])
        report = run_baseline(bank=bank, llm=baseline_llm)
        assert isinstance(report, BaselineReport)
        assert report.total == 2
        assert report.passed == 1
        assert report.pass_rate == 0.5

    def test_baseline_handles_llm_exceptions(self):
        def angry_llm(prompt: str, *, system: str = "") -> str:
            raise RuntimeError("no provider")

        bank = EvalBank(questions=[
            EvalQuestion(question="q", expected_keywords=["k"]),
        ])
        report = run_baseline(bank=bank, llm=angry_llm)
        # Graceful: no crash, just all fail.
        assert report.failed == 1
        assert report.results[0].answer == ""

    def test_compare_produces_per_question_comparison(self):
        # Axiom wins on Q1 (has materials), baseline wins on Q2 (LLM
        # knows general trivia), both fail Q3 (made-up term).
        bank = EvalBank(questions=[
            EvalQuestion(
                question="What is a control rod?",
                expected_keywords=["absorb"],
            ),
            EvalQuestion(
                question="What is the capital of France?",
                expected_keywords=["Paris"],
            ),
            EvalQuestion(
                question="What is XYZZY-42?",
                expected_keywords=["flux"],
            ),
        ])
        axiom = run_bank(
            bank=bank,
            retrieve=_fake_retriever,
            llm=_fake_llm_echo_citations,
        )

        def baseline_llm(prompt: str, *, system: str = "") -> str:
            if "capital of france" in prompt.lower():
                return "Paris."
            return "Not sure."

        baseline = run_baseline(bank=bank, llm=baseline_llm)
        comp = compare(axiom_report=axiom, baseline_report=baseline)
        assert isinstance(comp, ComparisonReport)
        assert comp.total == 3
        assert comp.axiom_only_wins == 1     # Q1
        assert comp.baseline_only_wins == 1  # Q2
        # lift = axiom passed minus baseline passed, over total
        #      = 1 - 1 over 3 = 0
        assert comp.lift == 0.0

    def test_compare_rejects_mismatched_banks(self):
        bank_a = EvalBank(questions=[
            EvalQuestion(question="q", expected_keywords=["k"]),
        ])
        bank_b = EvalBank(questions=[
            EvalQuestion(question="q1", expected_keywords=["k"]),
            EvalQuestion(question="q2", expected_keywords=["k"]),
        ])
        axiom = run_bank(
            bank=bank_a, retrieve=_fake_retriever, llm=_fake_llm_echo_citations,
        )
        baseline = run_baseline(bank=bank_b, llm=lambda p, *, system="": "x")
        with pytest.raises(ValueError, match="different lengths"):
            compare(axiom_report=axiom, baseline_report=baseline)

    def test_runner_records_zero_citations_on_no_match(self):
        bank = EvalBank(questions=[
            EvalQuestion(
                question="What color is the sky?",
                expected_keywords=["blue"],
            ),
        ])
        report = run_bank(
            bank=bank,
            retrieve=_fake_retriever,
            llm=_fake_llm_echo_citations,
        )
        # No citations, no synthesized answer, scored as failure — but
        # the run itself completed, no exception.
        assert report.results[0].citations == []
        assert report.failed == 1
