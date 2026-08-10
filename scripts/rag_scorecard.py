#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""RAG health/quality scorecard over a multi-dimension eval set.

Scores each question by its ``expected_behavior``:
  - answer  → grounded if the model's reply contains the expected fact(s)
              (notation-normalized substring); abstaining here is a MISS.
  - abstain → correct if the model refuses (score_abstention); answering
              an unanswerable question (hallucination) is a MISS.
Aggregates per dimension (tag) + overall. Read-only; safe during ingest.

  DATABASE_URL unused. Needs the gateway:
  RAG_EVAL_GATEWAY=http://localhost:8766 RAG_EVAL_KEY=... \
  python scripts/rag_scorecard.py --set ~/rag-eval-corpus-v2.yaml
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

from axiom.rag.eval import (
    load_questions,
    score_abstention,
    score_substring_normalized,
)

GATEWAY = os.environ.get("RAG_EVAL_GATEWAY", "http://localhost:8766")
KEY = os.environ.get("RAG_EVAL_KEY", os.environ.get("AXIOM_API_KEY", ""))
MODEL = os.environ.get("RAG_EVAL_MODEL", "rag-model")
JUDGE = os.environ.get("RAG_JUDGE_MODEL", MODEL)

# LLM-as-judge prompts. Keyword/substring scoring systematically under-counts
# correct free-form RAG behavior (the model refuses or answers in phrasings a
# fixed list never covers). A semantic judge grades intent, not surface form.
_JUDGE_ANSWER = (
    "Grade a retrieval system's answer. Reply with EXACTLY 'YES' or 'NO'.\n"
    "QUESTION: {q}\n"
    "The answer is correct if it conveys this expected fact: {exp}\n"
    "SYSTEM ANSWER: {ans}\n"
    "Does the system answer convey the expected fact? YES or NO:"
)
_JUDGE_ABSTAIN = (
    "Grade whether a retrieval system correctly DECLINED an unanswerable "
    "question (the fact is NOT in its corpus). Reply EXACTLY 'YES' or 'NO'.\n"
    "QUESTION: {q}\n"
    "SYSTEM ANSWER: {ans}\n"
    "Did it appropriately refuse / say it cannot answer / say not in the "
    "corpus, rather than fabricating a specific answer? YES or NO:"
)


def _judge(prompt: str) -> bool:
    """Ask the judge model a YES/NO grading question; True iff YES."""
    body = json.dumps({
        "model": JUDGE,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        f"{GATEWAY}/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        out = json.load(r)["choices"][0]["message"]["content"]
    return out.strip().upper().startswith("YES")


def _ask(question: str, timeout: int = 90) -> str:
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": question}],
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        f"{GATEWAY}/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["choices"][0]["message"]["content"]


def _dim(tags: list[str]) -> str:
    for t in ("single", "ocr", "multihop", "adversarial", "abstention"):
        if t in tags:
            return t
    return "other"


# Bare-LLM (no-retrieval) endpoint for the --compare baseline. Same question,
# NO corpus — isolates RAG's contribution. Defaults to a local Ollama OpenAI-
# compatible model; override to point at the same base model the gateway uses
# for an apples-to-apples comparison.
NORAG_URL = os.environ.get("RAG_NORAG_URL", "http://localhost:11434/v1")
NORAG_MODEL = os.environ.get("RAG_NORAG_MODEL", "qwen2.5:7b")
NORAG_KEY = os.environ.get("RAG_NORAG_KEY", "")


def _ask_norag(question: str, timeout: int = 90) -> str:
    """Answer with a bare LLM — no retrieval, no corpus context."""
    body = json.dumps({
        "model": NORAG_MODEL,
        "messages": [{"role": "user", "content": question}],
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        f"{NORAG_URL}/chat/completions", data=body,
        headers={"Authorization": f"Bearer {NORAG_KEY or 'x'}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["choices"][0]["message"]["content"]


def _score(q, ans: str, judge: bool, threshold: float) -> tuple[bool, str]:
    """Grade one answer for one question → (ok, metric)."""
    if q.expected_behavior == "abstain":
        ok = (_judge(_JUDGE_ABSTAIN.format(q=q.question, ans=ans)) if judge
              else score_abstention(ans) >= 1.0)
        return ok, ("refused" if ok else "HALLUCINATED")
    if judge:
        ok = _judge(_JUDGE_ANSWER.format(
            q=q.question, exp="; ".join(q.expected_answer_contains), ans=ans))
    else:
        ok = score_substring_normalized(ans, q.expected_answer_contains) >= threshold
    return ok, ("grounded" if ok else "missed")


def _report(title: str, by_dim: dict, n_rows: int) -> float:
    total_ok = sum(sum(v) for v in by_dim.values())
    print(f"\n=== {title} ===")
    for dim in sorted(by_dim):
        res = by_dim[dim]
        rate = sum(res) / len(res) if res else 0.0
        label = "abstention-correct" if dim == "abstention" else "grounded"
        print(f"  {dim:12} {sum(res):2}/{len(res):<2} {rate:4.0%}  ({label})")
    overall = total_ok / n_rows if n_rows else 0.0
    print(f"  {'OVERALL':12} {total_ok:2}/{n_rows:<2} {overall:4.0%}")
    return overall


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", required=True, help="eval YAML path")
    ap.add_argument("--threshold", type=float, default=0.5)
    ap.add_argument("--judge", action="store_true",
                    help="grade with an LLM judge (semantic) instead of "
                         "keyword/substring matching — far more reliable on "
                         "free-form answers")
    ap.add_argument("--compare", action="store_true",
                    help="also run a no-RAG baseline (bare LLM) and report the "
                         "RAG-vs-no-RAG delta — 'does RAG help, and how much?'")
    args = ap.parse_args()
    if not KEY:
        print("RAG_EVAL_KEY / AXIOM_API_KEY required", file=sys.stderr)
        return 2

    questions = load_questions(Path(args.set))
    rag_dim: dict[str, list[bool]] = defaultdict(list)
    norag_dim: dict[str, list[bool]] = defaultdict(list)
    n = 0
    for q in questions:
        dim = _dim(q.tags)
        try:
            ok, metric = _score(q, _ask(q.question), args.judge, args.threshold)
        except Exception as e:  # noqa: BLE001
            ok, metric = False, "error"
            print(f"  ERR {q.id}: {str(e)[:60]}", file=sys.stderr)
        rag_dim[dim].append(ok)
        n += 1
        line = f"  {'✓' if ok else '✗'} {q.id:16} {dim:11} RAG:{metric}"
        if args.compare:
            try:
                nok, nmetric = _score(q, _ask_norag(q.question), args.judge,
                                      args.threshold)
            except Exception:  # noqa: BLE001
                nok, nmetric = False, "error"
            norag_dim[dim].append(nok)
            line += f"  | noRAG:{nmetric}"
        print(line, file=sys.stderr)

    rag_overall = _report("RAG HEALTH SCORECARD (with retrieval)", rag_dim, n)
    if args.compare:
        norag_overall = _report("BASELINE (no retrieval / bare LLM)", norag_dim, n)
        delta = rag_overall - norag_overall
        print("\n=== RAG IMPACT ===")
        print(f"  with RAG:    {rag_overall:4.0%}")
        print(f"  without RAG: {norag_overall:4.0%}")
        print(f"  delta:       {delta:+.0%}  "
              f"(RAG {'helps' if delta > 0 else 'no measurable gain'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
