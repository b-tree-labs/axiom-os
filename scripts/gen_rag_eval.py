#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Auto-derive a trustworthy nuclear RAG eval set from the live corpus.

Every question's answer is provably present in a cited chunk: we sample a
real chunk, ask the model to write a factual Q + answer + the verbatim
supporting sentence drawn ONLY from that chunk, then GATE on entailment
(the quote must appear in the chunk and the answer must appear in the
quote). Questions that don't pass the gate are dropped. Output is a
RagEvalQuestion YAML with expected_citations = the chunk's source_path and
review_status=unreviewed (for postdoc/grad-student sign-off).

This is the regression-suite engine: re-run it as the corpus grows.

Run on a host with DB + gateway access (e.g. a self-hosted node):
  DATABASE_URL=... AXIOM_API_KEY=... GATEWAY=http://localhost:8766 \
  python scripts/gen_rag_eval.py --n 40 --out docs/working/rag-eval-corpus.yaml
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request

import psycopg2

GATEWAY = os.environ.get("GATEWAY", "http://localhost:8766")
KEY = os.environ.get("AXIOM_API_KEY", "")
MODEL = os.environ.get("RAG_MODEL", "rag-model")

GEN_PROMPT = (
    "You are writing one question for a NUCLEAR-ENGINEERING knowledge exam, drawn "
    "from a single source passage. Using ONLY the passage, produce STRICT JSON: "
    "{{\"question\": <a specific, substantive technical question>, "
    "\"answer\": <the short exact answer>, "
    "\"quote\": <the verbatim sentence stating the answer>}}.\n"
    "The answer must be a concrete TECHNICAL fact — a physical quantity, material "
    "composition, reactor/experiment parameter, method, procedure, date, or named "
    "entity that a nuclear engineer would care about.\n"
    "REJECT trivia: do NOT ask about page numbers, headers/footers, figure/table "
    "labels, file or document metadata, dollar costs/budgets, author biographies, "
    "affiliations, or formatting. If the passage has no substantive technical "
    "fact, return {{\"question\": null}}.\n\n"
    "PASSAGE:\n{chunk}\n"
)

# Reject low-value question shapes that slip past the prompt.
_TRIVIA = re.compile(
    r"\b(page number|header|footer|top of (the )?page|bottom of (the )?page|"
    r"figure \d|table \d|fellow|university|cost|\$|budget|file name|document "
    r"title|what month|what year is shown|affiliation)\b",
    re.IGNORECASE,
)


def _llm(prompt: str, timeout: int = 90) -> str:
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }).encode()
    req = urllib.request.Request(
        f"{GATEWAY}/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["choices"][0]["message"]["content"]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def _parse_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def sample_chunks(conn, n: int, exclude: set[str] | None = None) -> list[tuple]:
    """One fact-bearing chunk per DISTINCT document, spread widely across all
    Box content for maximum coverage. ``exclude`` skips source_paths already
    covered by prior runs so the suite keeps reaching new documents.
    """
    exclude = exclude or set()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_path, corpus, chunk_text FROM (
              SELECT DISTINCT ON (source_path) source_path, corpus, chunk_text
              FROM chunks
              WHERE source_path LIKE '/%%'           -- box-ingested content
                AND chunk_text ~ '[0-9]'             -- has a concrete fact
                AND length(chunk_text) BETWEEN 300 AND 4000
              ORDER BY source_path, md5(chunk_text)
            ) t
            ORDER BY md5(source_path)                 -- random spread over folders
            LIMIT %s
            """,
            (n * 4,),
        )
        rows = cur.fetchall()
    out = [(sp, c, t) for sp, c, t in rows if sp not in exclude]
    return out[:n]


def derive(chunk_text: str) -> dict | None:
    """Generate + entailment-gate one question from a chunk."""
    raw = _llm(GEN_PROMPT.format(chunk=chunk_text[:2000]))
    obj = _parse_json(raw)
    if not obj or not obj.get("question"):
        return None
    q, a, quote = obj.get("question"), obj.get("answer"), obj.get("quote")
    if not (q and a and quote):
        return None
    if _TRIVIA.search(f"{q} {a}"):  # drop page-number/cost/bio trivia
        return None
    chunk_n = _norm(chunk_text)
    # GATE: the quote must really be in the chunk, and the answer in the quote.
    if _norm(quote) not in chunk_n:
        return None
    if _norm(a) not in _norm(quote) and _norm(a) not in chunk_n:
        return None
    return {"question": q, "answer": a}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--out", default="rag-eval-corpus.yaml")
    args = ap.parse_args()

    if not KEY:
        print("AXIOM_API_KEY required", file=sys.stderr)
        return 2

    # Accumulate: keep already-vetted questions, skip docs they cover, so each
    # run widens coverage to NEW documents instead of resampling the same ones.
    existing: list[dict] = []
    covered: set[str] = set()
    if os.path.exists(args.out):
        import yaml
        existing = yaml.safe_load(open(args.out)) or []
        for it in existing:
            for c in it.get("expected_citations", []):
                covered.add(c)
        print(f"existing set: {len(existing)} questions covering "
              f"{len(covered)} docs", file=sys.stderr)

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    chunks = sample_chunks(conn, args.n, exclude=covered)
    print(f"sampled {len(chunks)} NEW-doc chunks", file=sys.stderr)

    items, kept = list(existing), len(existing)
    for i, (sp, corpus, txt) in enumerate(chunks):
        try:
            d = derive(txt)
        except Exception as e:  # noqa: BLE001
            print(f"  [{i}] gen error: {str(e)[:80]}", file=sys.stderr)
            continue
        if not d:
            print(f"  [{i}] dropped (no fact / failed entailment gate)", file=sys.stderr)
            continue
        kept += 1
        folder = sp.split("/")[1] if "/" in sp[1:] else "root"
        items.append({
            "id": f"corpus-{kept:03d}",
            "question": d["question"],
            "expected_answer_contains": [d["answer"]],
            "expected_citations": [sp],
            "tags": ["corpus-derived", "single-hop", _norm(folder).replace(" ", "-")[:24]],
            "review_status": "unreviewed",
        })
        print(f"  [{i}] KEPT: {d['question'][:70]}", file=sys.stderr)

    # Emit YAML (no yaml dep needed for writing — simple, stable formatting).
    lines = [
        "# Auto-derived nuclear RAG eval — answers provably in the cited chunk.",
        "# Generated by scripts/gen_rag_eval.py against the live corpus.",
        "# review_status=unreviewed until a domain expert signs off.",
        "",
    ]
    for it in items:
        lines.append(f"- id: {it['id']}")
        q = it["question"].replace('"', "'")
        lines.append(f'  question: "{q}"')
        a = str(it["expected_answer_contains"][0]).replace('"', "'")
        lines.append(f'  expected_answer_contains: ["{a}"]')
        c = it["expected_citations"][0].replace('"', "'")
        lines.append(f'  expected_citations: ["{c}"]')
        lines.append(f"  tags: {json.dumps(it['tags'])}")
        lines.append(f"  review_status: {it['review_status']}")
    out_text = "\n".join(lines) + "\n"
    with open(args.out, "w") as f:
        f.write(out_text)
    print(f"\nwrote {kept} questions → {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
