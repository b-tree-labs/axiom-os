# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Nuclear RAG corpus regression suite.

Loads the auto-derived, entailment-gated question set
(docs/working/rag-eval-corpus.yaml) — every answerable item's answer is
provably present in its cited source; every abstention item's missing entity
is provably absent — and runs it against a live gateway.

Three gates, matched to what the eval can honestly prove:

1. Per-question behavior. Answerable questions must surface the known fact;
   abstention questions must decline (not hallucinate from the training prior).

2. Citation surfaced when answering (soft). When the model answers correctly,
   it should reference the expected source. The chat endpoint returns prose,
   not structured citations, so this is a soft gate — but a retriever feeding
   the wrong documents shows up even when the prior happens to know the fact.

3. Aggregate DELTA, not an absolute. The robust assessment (#19) showed the
   absolute grounding number is eval-limited (needle-in-haystack exact values,
   near-misses); the trustworthy signal is that RAG beats the bare LLM on the
   same questions. The gate is "with-retrieval beats no-retrieval by a margin",
   which catches a real retriever/corpus regression without failing on the
   eval's inherent absolute ceiling.

Opt-in: requires a reachable gateway. Skips cleanly otherwise so unit CI
stays green.

  RAG_EVAL_GATEWAY=http://localhost:8766 RAG_EVAL_KEY=... \
    pytest tests/test_rag_corpus_regression.py -v
"""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

import pytest

from axiom.rag.eval import (
    load_questions,
    score_abstention,
    score_substring_normalized,
)

_GATEWAY = os.environ.get("RAG_EVAL_GATEWAY")
_KEY = os.environ.get("RAG_EVAL_KEY", "")
_MODEL = os.environ.get("RAG_EVAL_MODEL", "rag-model")
# Bare-LLM (retrieval-off) model id for the delta baseline. The gateway routes
# this to the same model without the RAG context block. Override per deployment.
_BASE_MODEL = os.environ.get("RAG_EVAL_BASE_MODEL", "bare-model")
_SET = Path(__file__).resolve().parents[1] / "docs" / "working" / "rag-eval-corpus.yaml"

pytestmark = pytest.mark.skipif(
    not _GATEWAY or not _SET.exists(),
    reason="RAG_EVAL_GATEWAY unset or corpus eval set missing — integration-only",
)


def _ask(question: str, model: str) -> str:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": question}],
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        f"{_GATEWAY}/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {_KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["choices"][0]["message"]["content"]


def _cite_basename(path: str) -> str:
    return path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()


def _passes(q, answer: str) -> bool:
    """Per-question pass, honoring expected_behavior."""
    if q.expected_behavior == "abstain":
        return score_abstention(answer) >= 1.0
    return score_substring_normalized(answer, q.expected_answer_contains) >= 0.5


_QUESTIONS = load_questions(_SET) if _SET.exists() else []
_ANSWERABLE = [q for q in _QUESTIONS if q.expected_behavior != "abstain"]


@pytest.mark.parametrize("q", _QUESTIONS, ids=[q.id for q in _QUESTIONS])
def test_corpus_question_behavior(q):
    """Each question exhibits the expected behavior (answer or abstain)."""
    answer = _ask(q.question, _MODEL)
    if q.expected_behavior == "abstain":
        assert score_abstention(answer) >= 1.0, (
            f"{q.id}: model should have abstained (corpus lacks the answer) "
            f"but answered confidently.\nGot: {answer[:300]}"
        )
    else:
        score = score_substring_normalized(answer, q.expected_answer_contains)
        assert score >= 0.5, (
            f"{q.id}: answer did not contain the known fact "
            f"{q.expected_answer_contains!r}.\nGot: {answer[:300]}"
        )


def test_citation_surfaced_when_answering():
    """When the model answers an answerable question correctly, it should
    reference the expected source. Soft gate: at least a third of
    correctly-answered cited items must name the expected source basename. A
    retriever returning the wrong documents shows up here even when the prior
    happens to contain the fact.
    """
    cited = correct = 0
    for q in _ANSWERABLE:
        if not q.expected_citations:
            continue
        try:
            ans = _ask(q.question, _MODEL)
        except Exception:  # noqa: BLE001
            continue
        if score_substring_normalized(ans, q.expected_answer_contains) < 0.5:
            continue
        correct += 1
        a = ans.lower()
        if any(_cite_basename(c) in a for c in q.expected_citations):
            cited += 1
    if correct == 0:
        pytest.skip("no correctly-answered cited questions to check")
    rate = cited / correct
    assert rate >= 0.33, (
        f"only {rate:.0%} of correct answers named the expected source "
        f"({cited}/{correct}) — retrieval may be surfacing wrong documents"
    )


def test_rag_beats_bare_llm():
    """The trustworthy aggregate gate: RAG must beat the bare LLM on the same
    questions. Per #19 the absolute is eval-limited; the DELTA is the signal.
    A retriever/corpus regression collapses the delta and fails here.
    """
    if not _QUESTIONS:
        pytest.skip("no questions loaded")
    rag_hits = bare_hits = scored = 0
    for q in _QUESTIONS:
        try:
            rag_ans = _ask(q.question, _MODEL)
            bare_ans = _ask(q.question, _BASE_MODEL)
        except Exception:  # noqa: BLE001 — a network blip drops the pair
            continue
        scored += 1
        rag_hits += int(_passes(q, rag_ans))
        bare_hits += int(_passes(q, bare_ans))
    if scored == 0:
        pytest.skip("no question pairs scored (gateway unreachable)")
    rag_rate = rag_hits / scored
    bare_rate = bare_hits / scored
    # Require a real positive lift, not just non-inferiority — RAG should add
    # grounding the prior lacks. 5 points is well inside the robust +12% margin
    # while tolerating run-to-run noise on a modest set.
    assert rag_rate >= bare_rate + 0.05, (
        f"RAG did not beat bare LLM: with-RAG {rag_rate:.0%} vs "
        f"bare {bare_rate:.0%} over {scored} questions (lift "
        f"{rag_rate - bare_rate:+.0%}). Expected a positive grounding lift."
    )
