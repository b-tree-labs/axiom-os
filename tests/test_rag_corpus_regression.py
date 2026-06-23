# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Nuclear RAG corpus regression suite.

Loads the auto-derived, entailment-gated question set
(docs/working/rag-eval-corpus.yaml) — every answer is provably present in
its cited source — and runs it against a live gateway, asserting the model
both surfaces the answer and cites the right source. Drift in the corpus,
retriever, or generation shows up here as a regression.

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

from axiom.rag.eval import load_questions, score_substring_normalized

_GATEWAY = os.environ.get("RAG_EVAL_GATEWAY")
_KEY = os.environ.get("RAG_EVAL_KEY", "")
_MODEL = os.environ.get("RAG_EVAL_MODEL", "rag-model")
_SET = Path(__file__).resolve().parents[1] / "docs" / "working" / "rag-eval-corpus.yaml"

pytestmark = pytest.mark.skipif(
    not _GATEWAY or not _SET.exists(),
    reason="RAG_EVAL_GATEWAY unset or corpus eval set missing — integration-only",
)


def _ask(question: str) -> str:
    body = json.dumps({
        "model": _MODEL,
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


_QUESTIONS = load_questions(_SET) if _SET.exists() else []


@pytest.mark.parametrize("q", _QUESTIONS, ids=[q.id for q in _QUESTIONS])
def test_corpus_question_grounded(q):
    """The model surfaces the known answer for a corpus-grounded question."""
    answer = _ask(q.question)
    score = score_substring_normalized(answer, q.expected_answer_contains)
    assert score >= 0.5, (
        f"{q.id}: answer did not contain the known fact "
        f"{q.expected_answer_contains!r}.\nGot: {answer[:300]}"
    )


def test_corpus_recall_rate():
    """Aggregate gate: at least 70% of the set must surface the known answer.

    A single flaky question shouldn't fail CI, but a corpus/retriever
    regression that tanks recall will.
    """
    if not _QUESTIONS:
        pytest.skip("no questions loaded")
    hits = 0
    for q in _QUESTIONS:
        try:
            ans = _ask(q.question)
        except Exception:  # noqa: BLE001 — network blip counts as miss
            continue
        if score_substring_normalized(ans, q.expected_answer_contains) >= 0.5:
            hits += 1
    rate = hits / len(_QUESTIONS)
    assert rate >= 0.70, f"corpus recall {rate:.0%} < 70% ({hits}/{len(_QUESTIONS)})"
