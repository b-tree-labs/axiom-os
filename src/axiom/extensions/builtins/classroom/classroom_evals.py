# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Classroom evals framework — question-bank runner + scoring.

Tier B piece. Lets an instructor (or a CI gate, eventually) run a
bank of questions through the classroom's Q&A pipeline and measure
how many answers contain the expected-concept keywords. First cut is
intentionally modest:

  - Keyword scoring (case-insensitive substring match on each keyword)
  - Single pipeline: the same ``answer_question`` engine the `ask`
    command uses
  - No baseline comparison yet — add a `--baseline=no-retrieval` flag
    in a follow-up once the framework feels stable

The runner is agnostic to where citations come from. Tests inject a
stub retriever; the CLI wraps the local classroom index.

Bank file format — JSONL, one question per line::

    {"question": "What is a control rod?",
     "expected_keywords": ["absorb", "neutron"]}

Blank lines skipped.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .classroom_qna import Citation, answer_question

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvalQuestion:
    question: str
    expected_keywords: list[str]
    category: str | None = None


@dataclass(frozen=True)
class EvalBank:
    questions: list[EvalQuestion]


@dataclass(frozen=True)
class KeywordScore:
    passed: bool
    hit_keywords: list[str]
    missed_keywords: list[str]


@dataclass(frozen=True)
class EvalResult:
    question: EvalQuestion
    answer: str
    citations: list[Citation]
    score: KeywordScore


@dataclass(frozen=True)
class EvalReport:
    results: list[EvalResult]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.score.passed)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    @property
    def pass_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.passed / self.total


# ---------------------------------------------------------------------------
# Bank loading
# ---------------------------------------------------------------------------


_REQUIRED_FIELDS = ("question", "expected_keywords")


def load_bank(path: Path) -> EvalBank:
    """Load a JSONL question bank from disk."""
    questions: list[EvalQuestion] = []
    with Path(path).open("r") as f:
        for i, raw_line in enumerate(f, 1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"bank line {i} not valid JSON: {exc}") from exc
            missing = [k for k in _REQUIRED_FIELDS if k not in obj]
            if missing:
                raise ValueError(
                    f"bank line {i} missing required field(s): {', '.join(missing)}"
                )
            questions.append(
                EvalQuestion(
                    question=str(obj["question"]),
                    expected_keywords=list(obj["expected_keywords"]),
                    category=obj.get("category"),
                )
            )
    return EvalBank(questions=questions)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def score_keywords(
    *,
    answer: str,
    expected_keywords: list[str],
) -> KeywordScore:
    """Case-insensitive substring match: every expected keyword must
    appear somewhere in the answer for a pass."""
    answer_lower = (answer or "").lower()
    hit: list[str] = []
    missed: list[str] = []
    for kw in expected_keywords:
        if kw.lower() in answer_lower:
            hit.append(kw)
        else:
            missed.append(kw)
    return KeywordScore(
        passed=not missed,
        hit_keywords=hit,
        missed_keywords=missed,
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


RetrieveFn = Callable[[str, int], list[Citation]]


def run_bank(
    *,
    bank: EvalBank,
    retrieve: RetrieveFn,
    llm,
    k: int = 3,
) -> EvalReport:
    """Run every question in ``bank`` through retrieve → answer → score.

    ``retrieve`` is any callable ``(question, k) -> list[Citation]`` —
    the CLI wraps the student's local index; tests can stub it.
    ``llm`` is the same shape as ``classroom_qna.answer_question``'s
    ``llm`` parameter.
    """
    results: list[EvalResult] = []
    for q in bank.questions:
        try:
            citations = retrieve(q.question, k)
        except Exception:
            citations = []

        qa = answer_question(
            question=q.question,
            citations=citations,
            llm=llm,
        )
        answer_text = qa.answer or ""  # empty string → score fails predictably
        score = score_keywords(
            answer=answer_text,
            expected_keywords=q.expected_keywords,
        )
        results.append(
            EvalResult(
                question=q,
                answer=answer_text,
                citations=list(citations),
                score=score,
            )
        )
    return EvalReport(results=results)


# ---------------------------------------------------------------------------
# Baseline — same LLM, NO retrieval. Quantifies the vector+graph lift.
# ---------------------------------------------------------------------------


_BASELINE_SYSTEM_PROMPT = (
    "Answer the question concisely from your general knowledge. "
    "No preamble, just the answer."
)


@dataclass(frozen=True)
class BaselineResult:
    question: EvalQuestion
    answer: str
    score: KeywordScore


@dataclass(frozen=True)
class BaselineReport:
    results: list[BaselineResult]

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.score.passed)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    @property
    def pass_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.passed / self.total


def run_baseline(
    *,
    bank: EvalBank,
    llm,
) -> BaselineReport:
    """Run each question through the LLM with NO retrieval context.

    The point is to isolate the lift that the class materials + local
    index provide: baseline pass rate is how well the bare LLM does
    from world knowledge alone. Axiom's pass rate minus baseline is
    the honest number we claim.
    """
    results: list[BaselineResult] = []
    for q in bank.questions:
        try:
            text = llm(q.question, system=_BASELINE_SYSTEM_PROMPT)
        except Exception:
            text = None
        answer = (text or "").strip()
        score = score_keywords(
            answer=answer,
            expected_keywords=q.expected_keywords,
        )
        results.append(
            BaselineResult(question=q, answer=answer, score=score)
        )
    return BaselineReport(results=results)


@dataclass(frozen=True)
class Comparison:
    """Side-by-side results for one question."""

    question: EvalQuestion
    axiom: EvalResult
    baseline: BaselineResult

    @property
    def axiom_wins(self) -> bool:
        return self.axiom.score.passed and not self.baseline.score.passed

    @property
    def baseline_wins(self) -> bool:
        return self.baseline.score.passed and not self.axiom.score.passed


@dataclass(frozen=True)
class ComparisonReport:
    comparisons: list[Comparison]

    @property
    def total(self) -> int:
        return len(self.comparisons)

    @property
    def axiom_passed(self) -> int:
        return sum(1 for c in self.comparisons if c.axiom.score.passed)

    @property
    def baseline_passed(self) -> int:
        return sum(1 for c in self.comparisons if c.baseline.score.passed)

    @property
    def axiom_only_wins(self) -> int:
        return sum(1 for c in self.comparisons if c.axiom_wins)

    @property
    def baseline_only_wins(self) -> int:
        return sum(1 for c in self.comparisons if c.baseline_wins)

    @property
    def lift(self) -> float:
        """Axiom pass rate minus baseline pass rate (positive = retrieval helps)."""
        if self.total == 0:
            return 0.0
        return (self.axiom_passed - self.baseline_passed) / self.total


def compare(
    *,
    axiom_report: EvalReport,
    baseline_report: BaselineReport,
) -> ComparisonReport:
    """Zip two parallel reports over the same bank into a comparison."""
    if len(axiom_report.results) != len(baseline_report.results):
        raise ValueError(
            "axiom and baseline reports have different lengths — "
            "were they run on the same bank?"
        )
    comparisons = [
        Comparison(question=a.question, axiom=a, baseline=b)
        for a, b in zip(axiom_report.results, baseline_report.results)
    ]
    return ComparisonReport(comparisons=comparisons)


__all__ = [
    "BaselineReport",
    "BaselineResult",
    "Comparison",
    "ComparisonReport",
    "EvalBank",
    "EvalQuestion",
    "EvalReport",
    "EvalResult",
    "KeywordScore",
    "RetrieveFn",
    "compare",
    "load_bank",
    "run_bank",
    "run_baseline",
    "score_keywords",
]
