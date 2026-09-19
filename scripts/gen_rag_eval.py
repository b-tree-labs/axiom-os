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
    "The answer must be a concrete TECHNICAL fact — a DESIGN parameter, material "
    "composition, experiment configuration, method, procedure, or named entity "
    "that a nuclear engineer would care about. Prefer durable document facts "
    "(how something is built, what it is made of, how a procedure works).\n"
    "REJECT trivia: do NOT ask about page numbers, headers/footers, figure/table "
    "labels, file or document metadata, dollar costs/budgets, author biographies, "
    "affiliations, or formatting.\n"
    "REJECT telemetry: do NOT ask for an instantaneous reading at a specific "
    "time or date (e.g. the power level on a given day, a logged temperature at "
    "a timestamp) — those are time-series data, not document facts.\n"
    "If the passage has no substantive durable technical fact, return "
    "{{\"question\": null}}.\n\n"
    "PASSAGE:\n{chunk}\n"
)

# Reject low-value question shapes that slip past the prompt.
_TRIVIA = re.compile(
    r"\b(page number|header|footer|top of (the )?page|bottom of (the )?page|"
    r"figure \d|table \d|fellow|university|cost|\$|budget|file name|document "
    r"title|what month|what year is shown|affiliation)\b",
    re.IGNORECASE,
)

# Telemetry / time-series lookups belong in the structured gold tier, not
# document-RAG. sample_chunks() drops telemetry FILES, but operating reports
# quote instantaneous readings inline ("max power on 9 Aug 2004 was 0.602 MW").
# A single-hop question over that prose is a data-ROUTING test (RAG should defer
# to the time-series tier), not a document-RAG quality test — and scoring RAG's
# correct refusal as a miss is the eval-design pollution that depressed the
# robust absolute. Drop any question that is an instantaneous-value-at-a-given-
# time/-date lookup so the single-hop dimension measures document grounding.
_TELEMETRY_Q = re.compile(
    r"\b(on|at|during|for)\b.{0,40}\b("
    r"\d{1,2}[:/]\d{2}"                       # a clock time or m/d
    r"|\d{1,2}\s+\w+\s+\d{4}"                 # "9 August 2004"
    r"|\w+\s+\d{1,2},?\s+\d{4}"               # "August 9, 2004"
    r"|\d{4}-\d{2}-\d{2}"                     # ISO date
    r")\b",
    re.IGNORECASE,
)
# A bare "<number> <unit>" answer is an instantaneous physical reading
# (telemetry), as opposed to a design parameter or a material/method fact.
_TELEMETRY_UNITS = re.compile(
    r"^\s*[-+]?\d+(\.\d+)?\s*"
    r"(mw|kw|w|mwth|°?c|degc|psi|psig|bar|pa|kpa|mpa|gpm|lpm|"
    r"rpm|hz|ma|ppm)\s*$",
    re.IGNORECASE,
)
# Snapshot phrasing that, combined with a units-only answer, marks telemetry.
_SNAPSHOT_Q = re.compile(
    r"\b(reading|recorded|logged|measured|at the time|that day|snapshot|"
    r"instantaneous|console|log entry)\b",
    re.IGNORECASE,
)


def _is_telemetry_question(q: str, a: str) -> bool:
    """True when the Q/A pair is an instantaneous time-series reading rather
    than a document fact — those belong in the structured tier (see #19/#23).

    Two signals: (1) the question asks for a value AT a specific time/date;
    (2) the answer is a bare "<number> <unit>" reading AND the question uses
    snapshot phrasing. A units answer alone (e.g. a rated-power design spec)
    is a legitimate document fact and is NOT filtered.
    """
    if _TELEMETRY_Q.search(q):
        return True
    if _TELEMETRY_UNITS.match(a or "") and _SNAPSHOT_Q.search(q):
        return True
    return False


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


def sample_chunks(conn, n: int, exclude: set[str] | None = None,
                  source_type: str | None = None) -> list[tuple]:
    """One fact-bearing chunk per DISTINCT document, spread widely across all
    Box content for maximum coverage. ``exclude`` skips source_paths already
    covered by prior runs so the suite keeps reaching new documents.
    ``source_type`` (e.g. ``'pdf'``) restricts to one extraction kind — used
    by the OCR dimension to target scanned/PDF-extracted content.
    """
    exclude = exclude or set()
    # When filtering by source_type (the OCR dimension), don't also require the
    # leading-slash box path: OCR/pdf-extracted docs land under non-slash paths
    # (e.g. "1980-1989/…ML….pdf"), so the box-path filter would exclude them.
    if source_type:
        path_clause = "source_path IS NOT NULL"
        type_clause = "AND source_type = %s"
        params: tuple = (source_type, n * 4)
    else:
        path_clause = "source_path LIKE '/%%'"   # box-ingested content
        type_clause = ""
        params = (n * 4,)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT source_path, corpus, chunk_text FROM (
              SELECT DISTINCT ON (source_path) source_path, corpus, chunk_text
              FROM chunks
              WHERE {path_clause}
                AND chunk_text ~ '[0-9]'             -- has a concrete fact
                AND length(chunk_text) BETWEEN 300 AND 4000
                -- PROSE-ONLY: exclude reactor telemetry (it belongs in the
                -- time-series gold tier, not RAG; evaluating RAG on it is a
                -- routing mismatch, not a quality signal).
                AND source_path NOT ILIKE '%%console%%'
                AND source_path NOT ILIKE '%%status.txt'
                AND source_path NOT ILIKE '%%.csv'
                AND source_path NOT ILIKE '%%/crh%%'
                {type_clause}
              ORDER BY source_path, md5(chunk_text)
            ) t
            ORDER BY md5(source_path)                 -- random spread over folders
            LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()
    out = [(sp, c, t) for sp, c, t in rows if sp not in exclude]
    return out[:n]


def sample_pairs(conn, n: int, exclude: set[str] | None = None) -> list[tuple]:
    """Sample ``n`` TOPICALLY-RELATED cross-corpus chunk pairs for multi-hop
    questions. For each random seed chunk we find its nearest neighbour (by
    embedding cosine distance) in a DIFFERENT top-level source folder — so the
    two chunks actually share subject matter and a real combined question
    exists (random cross-corpus pairing almost never relates).
    Returns list of ((sp_a, txt_a), (sp_b, txt_b))."""
    exclude = exclude or set()
    out: list[tuple] = []
    # Sample random seed chunks (with embeddings), then for each find its
    # nearest cross-folder neighbour via the ivfflat index.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_path, chunk_text, embedding FROM (
              SELECT DISTINCT ON (source_path) source_path, chunk_text, embedding
              FROM chunks
              WHERE source_path LIKE '/%%' AND chunk_text ~ '[0-9]'
                AND length(chunk_text) BETWEEN 300 AND 4000
              ORDER BY source_path, md5(chunk_text)
            ) t ORDER BY md5(source_path) LIMIT %s
            """,
            (n * 3,),
        )
        seeds = [r for r in cur.fetchall() if r[0] not in exclude]
        for sp_a, txt_a, emb_a in seeds:
            cur.execute(
                """
                SELECT source_path, chunk_text FROM chunks
                WHERE source_path LIKE '/%%'
                  AND split_part(source_path,'/',2) <> split_part(%s,'/',2)
                  AND chunk_text ~ '[0-9]'
                  AND length(chunk_text) BETWEEN 300 AND 4000
                ORDER BY embedding <=> %s
                LIMIT 1
                """,
                (sp_a, emb_a),
            )
            row = cur.fetchone()
            if row and row[0] not in exclude:
                out.append(((sp_a, txt_a), (row[0], row[1])))
            if len(out) >= n:
                break
    return out


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
    if _is_telemetry_question(q, a):  # drop time-series-value lookups (#19/#23)
        return None
    chunk_n = _norm(chunk_text)
    # GATE: the quote must really be in the chunk, and the answer in the quote.
    if _norm(quote) not in chunk_n:
        return None
    if _norm(a) not in _norm(quote) and _norm(a) not in chunk_n:
        return None
    return {"question": q, "answer": a}


# --- multi-hop / cross-corpus -------------------------------------------------

MULTIHOP_PROMPT = (
    "Here are TWO independently-verified nuclear-engineering facts from "
    "different documents:\n  FACT A — Q: {qa}  A: {aa}\n  FACT B — Q: {qb}  "
    "A: {ab}\n\nWrite ONE question that can only be answered by using BOTH "
    "facts — comparing, relating, or combining them — so neither alone "
    "suffices. The correct answer must reference both {aa} and {ab}. Return "
    "STRICT JSON: {{\"question\": <the combined question>}}. If the two facts "
    "have no meaningful technical relationship, return {{\"question\": null}}.\n"
)


def derive_multihop(text_a: str, text_b: str) -> dict | None:
    """Compose a cross-document question from TWO already-entailment-gated
    single-hop facts (one per chunk). More robust than one-shot dual-quoting:
    each fact is independently proven, and the answer must contain BOTH."""
    fa = derive(text_a)
    fb = derive(text_b)
    if not fa or not fb:
        return None
    raw = _llm(MULTIHOP_PROMPT.format(qa=fa["question"], aa=fa["answer"],
                                      qb=fb["question"], ab=fb["answer"]))
    obj = _parse_json(raw)
    if not obj or not obj.get("question"):
        return None
    q = obj["question"]
    if _TRIVIA.search(q):
        return None
    return {"question": q, "answers": [fa["answer"], fb["answer"]]}


# --- adversarial (false premise) ---------------------------------------------

ADVERSARIAL_PROMPT = (
    "Here is a TRUE nuclear-engineering fact:\n  Q: {q}\n  A: {a}\n\n"
    "Write a NEW question that embeds a FALSE premise contradicting this fact "
    "(e.g. asserts the wrong material, value, or mechanism) and asks the reader "
    "to explain or elaborate on the false premise. A grounded system must REFUSE "
    "the premise or correct it, not play along. Return STRICT JSON: "
    "{{\"question\": <the false-premise question>, \"correction\": <the true "
    "fact that refutes it>}}.\n"
)


def derive_adversarial(true_q: str, true_a: str) -> dict | None:
    """From a verified true fact, craft a false-premise question; the correct
    behavior is to refute with the true fact."""
    raw = _llm(ADVERSARIAL_PROMPT.format(q=true_q, a=true_a))
    obj = _parse_json(raw)
    if not obj or not obj.get("question") or not obj.get("correction"):
        return None
    return {"question": obj["question"], "answer": true_a,
            "correction": obj["correction"]}


# --- abstention (not in corpus) ----------------------------------------------

ABSTENTION_PROMPT = (
    "Based on this nuclear-engineering passage, invent ONE plausible, specific "
    "technical question about a DIFFERENT detail that is NOT stated in the "
    "passage and a reader could not answer from it (a fabricated-but-realistic "
    "parameter, batch, date, or sub-component). Return STRICT JSON: "
    "{{\"question\": <the question>, \"missing_entity\": <the distinctive term "
    "whose absence makes it unanswerable>}}.\n\nPASSAGE:\n{chunk}\n"
)


def derive_abstention(conn, chunk_text: str) -> dict | None:
    """Generate an unanswerable question, then VERIFY absence: the distinctive
    entity must not appear anywhere in the corpus. The system should abstain."""
    raw = _llm(ABSTENTION_PROMPT.format(chunk=chunk_text[:1600]))
    obj = _parse_json(raw)
    if not obj or not obj.get("question") or not obj.get("missing_entity"):
        return None
    q, ent = obj["question"], str(obj["missing_entity"]).strip()
    if len(ent) < 4:
        return None
    # GATE absence: the distinctive entity must not be present in any chunk.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM chunks WHERE chunk_text ILIKE %s LIMIT 1",
            (f"%{ent}%",),
        )
        if cur.fetchone():
            return None  # actually present → not a valid abstention probe
    return {"question": q, "answer": ent}


_MODES = ("single", "ocr", "multihop", "adversarial", "abstention")


def _fold(sp: str) -> str:
    folder = sp.split("/")[1] if "/" in sp[1:] else "root"
    return _norm(folder).replace(" ", "-")[:24]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--out", default="rag-eval-corpus.yaml")
    ap.add_argument("--mode", default="single", choices=_MODES,
                    help="dimension to generate (default single-hop)")
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
            for c in it.get("expected_citations", []) or []:
                covered.add(c)
        print(f"existing set: {len(existing)} questions covering "
              f"{len(covered)} docs", file=sys.stderr)

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    items, kept = list(existing), len(existing)
    mode = args.mode

    def add(question, answers, citations, *, behavior, extra_tags):
        nonlocal kept
        kept += 1
        items.append({
            "id": f"{mode}-{kept:03d}",
            "question": question,
            "expected_answer_contains": answers,
            "expected_citations": citations,
            "expected_behavior": behavior,
            "tags": ["corpus-derived", mode, *extra_tags],
            "review_status": "unreviewed",
        })

    if mode == "multihop":
        pairs = sample_pairs(conn, args.n, exclude=covered)
        print(f"sampled {len(pairs)} cross-corpus pairs", file=sys.stderr)
        for i, ((spa, ta), (spb, tb)) in enumerate(pairs):
            try:
                d = derive_multihop(ta, tb)
            except Exception as e:  # noqa: BLE001
                print(f"  [{i}] gen error: {str(e)[:80]}", file=sys.stderr)
                continue
            if not d:
                print(f"  [{i}] dropped (no cross-fact / gate)", file=sys.stderr)
                continue
            add(d["question"], d["answers"], [spa, spb],
                behavior="answer", extra_tags=["multi-hop", "cross-corpus"])
            print(f"  [{i}] KEPT: {d['question'][:70]}", file=sys.stderr)
    else:
        stype = "pdf" if mode == "ocr" else None
        chunks = sample_chunks(conn, args.n, exclude=covered, source_type=stype)
        print(f"sampled {len(chunks)} NEW-doc chunks (mode={mode})", file=sys.stderr)
        for i, (sp, _corpus, txt) in enumerate(chunks):
            try:
                if mode == "abstention":
                    d = derive_abstention(conn, txt)
                    if d:
                        add(d["question"], [], [],
                            behavior="abstain", extra_tags=["unanswerable", _fold(sp)])
                elif mode == "adversarial":
                    base = derive(txt)
                    d = derive_adversarial(base["question"], base["answer"]) if base else None
                    if d:
                        add(d["question"], [d["answer"]], [sp],
                            behavior="answer", extra_tags=["false-premise", _fold(sp)])
                else:  # single | ocr
                    d = derive(txt)
                    if d:
                        add(d["question"], [d["answer"]], [sp], behavior="answer",
                            extra_tags=(["single-hop"] if mode == "single"
                                        else ["ocr"]) + [_fold(sp)])
            except Exception as e:  # noqa: BLE001
                print(f"  [{i}] gen error: {str(e)[:80]}", file=sys.stderr)
                continue
            if not d:
                print(f"  [{i}] dropped (no fact / failed gate)", file=sys.stderr)
                continue
            print(f"  [{i}] KEPT: {d['question'][:70]}", file=sys.stderr)

    # Emit YAML (no yaml dep needed for writing — simple, stable formatting).
    lines = [
        "# Auto-derived nuclear RAG eval — multi-dimension (single/ocr/multihop/",
        "# adversarial/abstention). Answer-bearing items are entailment-gated to a",
        "# cited chunk; abstention items are absence-gated (entity not in corpus).",
        "# review_status=unreviewed until a domain expert signs off.",
        "",
    ]
    for it in items:
        lines.append(f"- id: {it['id']}")
        q = it["question"].replace('"', "'")
        lines.append(f'  question: "{q}"')
        ans = [str(a).replace('"', "'") for a in it.get("expected_answer_contains", [])]
        lines.append(f"  expected_answer_contains: {json.dumps(ans)}")
        cites = [c.replace('"', "'") for c in it.get("expected_citations", []) or []]
        lines.append(f"  expected_citations: {json.dumps(cites)}")
        lines.append(f"  expected_behavior: {it.get('expected_behavior', 'answer')}")
        lines.append(f"  tags: {json.dumps(it['tags'])}")
        lines.append(f"  review_status: {it['review_status']}")
    out_text = "\n".join(lines) + "\n"
    with open(args.out, "w") as f:
        f.write(out_text)
    print(f"\nwrote {kept} questions ({mode}) → {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
