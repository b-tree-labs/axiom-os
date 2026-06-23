# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RAG eval harness — value proof for 'LLM with RAG vs LLM baseline.'

Module-agnostic: any ``Callable[[prompt, context=None], str]`` is a
model, any ``Callable[[query], list[Citation]]`` is a retriever, and
any sequence of :class:`RagEvalQuestion` is an eval set. The
:func:`compare_with_and_without_retrieval` helper is the headline
shape — produces a structured diff Ben can show teammates.

Scoring is deliberately simple in v0:

- ``score_substring`` — fraction of required phrases present in the
  answer (case-insensitive).
- ``score_citation_overlap`` — Jaccard between returned citations and
  expected citations; empty expected → 1.0 (don't penalize when the
  question doesn't require sources).

v1 will add LLM-judge faithfulness scoring and ROUGE-like content
overlap; v0 is enough to establish baselines without arguing about
metric design first.

CLI surface lands as ``axi rag eval --questions <file.yaml>
--model qwen [--no-retrieval]`` in a follow-up; this module is the
library that surface will call.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean

# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Citation:
    """One retrieval hit's identity for scoring + display."""

    source_path: str
    chunk_text: str = ""
    chunk_index: int = 0
    score: float = 0.0


@dataclass(frozen=True)
class RagEvalQuestion:
    """One eval question + its acceptance criteria."""

    id: str
    question: str
    expected_answer_contains: list[str] = field(default_factory=list)
    expected_citations: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    # "answer" (default) → grade against expected_answer_contains.
    # "abstain" → the corpus does NOT contain the answer; the model must
    # decline ("not in the corpus"), not answer from its training prior.
    # This is the adversarial-absence / hallucination-resistance case.
    expected_behavior: str = "answer"
    # Optional review provenance for the auto-derive → expert-review flow.
    # "unreviewed" until a postdoc/grad-student signs off on the gold.
    review_status: str = "unreviewed"


@dataclass(frozen=True)
class RagEvalRun:
    """The model's output on one question, scored."""

    question_id: str
    answer: str
    citations: list[Citation]
    retrieval_enabled: bool
    answer_score: float           # 0..1
    citation_score: float          # 0..1
    latency_ms: int


@dataclass(frozen=True)
class RagEvalReport:
    """Aggregate over a batch of runs."""

    runs: list[RagEvalRun]
    total: int
    passed: int                    # runs with answer_score == 1.0
    mean_answer_score: float
    mean_citation_score: float
    mean_latency_ms: int


@dataclass(frozen=True)
class RagEvalDiff:
    """Baseline vs with-retrieval comparison — the headline shape."""

    baseline: RagEvalReport
    with_retrieval: RagEvalReport
    lift: float                    # delta in mean_answer_score


# ---------------------------------------------------------------------------
# Scoring primitives
# ---------------------------------------------------------------------------


def score_substring(answer: str, required: Iterable[str]) -> float:
    """Fraction of ``required`` phrases present in ``answer``
    (case-insensitive substring match). Empty ``required`` → 1.0."""
    required_list = list(required)
    if not required_list:
        return 1.0
    a = answer.lower()
    hits = sum(1 for phrase in required_list if phrase.lower() in a)
    return hits / len(required_list)


_SUBSCRIPT = str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789")
_SUPERSCRIPT = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")


def normalize_notation(text: str) -> str:
    """Fold scientific notation so substring matching isn't defeated by
    typography: unicode sub/superscripts → ASCII digits, lowercased.

    Without this, an answer saying "LiF-BeF₂" scores 0 against the
    required key "BeF2" — a scorer artifact, not a grounding failure
    (observed on the nuclear-v0 run).
    """
    return text.translate(_SUBSCRIPT).translate(_SUPERSCRIPT).lower()


def score_substring_normalized(answer: str, required: Iterable[str]) -> float:
    """Like :func:`score_substring` but notation-normalized on both sides."""
    required_list = list(required)
    if not required_list:
        return 1.0
    a = normalize_notation(answer)
    hits = sum(1 for phrase in required_list if normalize_notation(phrase) in a)
    return hits / len(required_list)


# Phrases that signal the model correctly declined to answer from a corpus
# that does not contain the fact (adversarial-absence questions).
_ABSTENTION_MARKERS = (
    "not in the corpus",
    "not found in the",
    "no information",
    "could not find",
    "couldn't find",
    "i don't find",
    "i do not find",
    "does not appear",
    "not available in",
    "no relevant",
    "cannot find",
    "can't find",
    "not indexed",
    "no documents",
)


def score_abstention(answer: str) -> float:
    """1.0 if the answer correctly abstains, else 0.0.

    For adversarial-absence questions the corpus does NOT contain the
    answer; a confident answer is a hallucination (the failure mode the
    grounding + tool-loop fixes target). For a nuclear domain a wrong
    confident answer is worse than an honest "not in the corpus", so this
    is graded as a hard pass/fail, not partial credit.
    """
    a = normalize_notation(answer)
    return 1.0 if any(m in a for m in _ABSTENTION_MARKERS) else 0.0


def score_citation_overlap(
    got: Iterable[Citation], expected: Iterable[str],
) -> float:
    """Jaccard between returned citations' source_paths and expected
    source_paths. Empty expected → 1.0."""
    expected_set = set(expected)
    if not expected_set:
        return 1.0
    got_set = {c.source_path for c in got}
    if not got_set:
        return 0.0
    inter = expected_set & got_set
    union = expected_set | got_set
    return len(inter) / len(union)


# ---------------------------------------------------------------------------
# Eval runner
# ---------------------------------------------------------------------------


ModelFn = Callable[..., str]
RetrieverFn = Callable[[str], list[Citation]]


def run_eval(
    questions: Iterable[RagEvalQuestion],
    *,
    model_fn: ModelFn,
    retriever_fn: RetrieverFn | None,
) -> RagEvalReport:
    """Execute ``questions`` against ``model_fn`` (optionally with
    ``retriever_fn``) and return a scored report.

    The model callable signature is permissive: it should accept
    ``(prompt, context=None)`` — if ``context`` is provided, it's the
    concatenated chunk text from the retriever. Models that don't take
    a context kwarg can ignore it (they'll just see the prompt).
    """
    runs: list[RagEvalRun] = []
    for q in questions:
        citations: list[Citation] = []
        context: str | None = None
        if retriever_fn is not None:
            citations = list(retriever_fn(q.question))
            context = "\n\n".join(c.chunk_text for c in citations if c.chunk_text)

        t0 = time.monotonic()
        try:
            answer = model_fn(q.question, context=context)
        except TypeError:
            # Model doesn't accept a context kwarg; fall back to bare prompt.
            answer = model_fn(q.question)
        latency_ms = int((time.monotonic() - t0) * 1000)

        runs.append(RagEvalRun(
            question_id=q.id,
            answer=answer,
            citations=citations,
            retrieval_enabled=retriever_fn is not None,
            answer_score=score_substring(answer, q.expected_answer_contains),
            citation_score=score_citation_overlap(citations, q.expected_citations),
            latency_ms=latency_ms,
        ))

    return _aggregate(runs)


def compare_with_and_without_retrieval(
    questions: Iterable[RagEvalQuestion],
    *,
    model_fn: ModelFn,
    retriever_fn: RetrieverFn,
) -> RagEvalDiff:
    """Run the same model + question set twice — once with retrieval,
    once without — and return a diff. The headline 'does RAG help?'
    shape."""
    qs = list(questions)
    baseline = run_eval(qs, model_fn=model_fn, retriever_fn=None)
    with_rag = run_eval(qs, model_fn=model_fn, retriever_fn=retriever_fn)
    return RagEvalDiff(
        baseline=baseline,
        with_retrieval=with_rag,
        lift=with_rag.mean_answer_score - baseline.mean_answer_score,
    )


def _aggregate(runs: list[RagEvalRun]) -> RagEvalReport:
    if not runs:
        return RagEvalReport(runs=[], total=0, passed=0,
                             mean_answer_score=0.0,
                             mean_citation_score=0.0, mean_latency_ms=0)
    return RagEvalReport(
        runs=runs,
        total=len(runs),
        passed=sum(1 for r in runs if r.answer_score == 1.0),
        mean_answer_score=mean(r.answer_score for r in runs),
        mean_citation_score=mean(r.citation_score for r in runs),
        mean_latency_ms=int(mean(r.latency_ms for r in runs)),
    )


# ---------------------------------------------------------------------------
# Question-set IO
# ---------------------------------------------------------------------------


def load_questions(path: Path) -> list[RagEvalQuestion]:
    """Load a YAML question set. Each item:

    ::

        - id: q1
          question: "What moderator did CP2 use?"
          expected_answer_contains: ["graphite"]
          expected_citations: ["cp2.pdf"]
          tags: ["history", "reactors"]
    """
    import yaml

    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, list):
        raise ValueError(f"question file must be a YAML list of dicts: {path}")

    out: list[RagEvalQuestion] = []
    for entry in raw:
        out.append(RagEvalQuestion(
            id=str(entry["id"]),
            question=str(entry["question"]),
            expected_answer_contains=list(entry.get("expected_answer_contains") or []),
            expected_citations=list(entry.get("expected_citations") or []),
            tags=list(entry.get("tags") or []),
            expected_behavior=str(entry.get("expected_behavior") or "answer"),
            review_status=str(entry.get("review_status") or "unreviewed"),
        ))
    return out


__all__ = [
    "Citation",
    "RagEvalQuestion",
    "RagEvalRun",
    "RagEvalReport",
    "RagEvalDiff",
    "score_substring",
    "score_citation_overlap",
    "run_eval",
    "compare_with_and_without_retrieval",
    "load_questions",
]
