#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Is the corpus healthy, and is retrieval serving all of it?

Two halves, because they fail differently. **Structure** asks what is in the
index and what can never be reached — rows with no embedding are invisible to
the dense arm, rows whose tsvector is empty are invisible to the sparse one,
and a document whose every chunk was refused is invisible to both while still
appearing in the catalogue. **Retrieval** asks whether real questions come back
with the right documents, through both arms.

**It builds its queries the way the store does.** An earlier version of this
battery used `websearch_to_tsquery`, which ANDs every term, and reported a
false failure on a question the system answers correctly — the store uses
OR-of-terms precisely so a multi-term question recalls candidates. A battery
that does not reproduce the real query construction measures itself.

    python rag_battery.py --pod <postgres-pod> [--embed-url URL]

**Runs where the cluster is.** It shells out to `kubectl exec`, and the
embedder URL defaults to localhost, so this is a node-side script. Run from a
workstation it produces an empty report rather than an error, which is why the
structure section prints its row count.

`--structure-only` needs nothing but kubectl. The retrieval half additionally
imports `axiom.rag.fts_query`, to build queries the way the store does.

Emits a report and exits non-zero if an arm returns nothing for any question,
which is the failure worth waking someone for.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.request

DEFAULT_QUERIES = [
    "What is the licensed maximum power level of the TRIGA reactor?",
    "How is control rod worth measured during a calibration?",
    "What are the technical specifications for coolant temperature limits?",
    "Describe the emergency shutdown procedure for the reactor",
    "What fuel element inspection requirements apply?",
    "How does the beam port shielding work?",
    "What neutron flux measurements were taken at the central thimble?",
    "What are the requirements for reactor operator requalification?",
]

STRUCTURE_SQL = """
SELECT 'corpora',        string_agg(DISTINCT corpus, ', ')              FROM chunks
UNION ALL SELECT 'chunks',          count(*)::text                      FROM chunks
UNION ALL SELECT 'documents',       count(DISTINCT source_path)::text   FROM chunks
UNION ALL SELECT 'no embedding (dense-invisible)', count(*)::text       FROM chunks WHERE embedding IS NULL
UNION ALL SELECT 'empty tsvector (sparse-invisible)', count(*)::text    FROM chunks
          WHERE to_tsvector('english', chunk_text) = ''::tsvector
UNION ALL SELECT 'restricted (gated out by default)', count(*)::text    FROM chunks
          WHERE access_tier <> 'public' OR classification <> 'unclassified'
UNION ALL SELECT 'catalogued but chunkless', count(*)::text FROM documents d
          WHERE NOT EXISTS (SELECT 1 FROM chunks c WHERE c.source_path = d.source_path)
UNION ALL SELECT 'invalid indexes', count(*)::text FROM pg_index WHERE NOT indisvalid
UNION ALL SELECT 'ivfflat.probes', current_setting('ivfflat.probes')
UNION ALL SELECT 'embedding type', (SELECT udt_name FROM information_schema.columns
          WHERE table_name='chunks' AND column_name='embedding');
"""


def psql(pod: str, sql: str) -> str:
    with open("/tmp/_battery.sql", "w") as fh:
        fh.write(sql)
    subprocess.run(["kubectl", "cp", "/tmp/_battery.sql", f"axiom/{pod}:/tmp/_battery.sql"],
                   capture_output=True)
    return subprocess.run(
        ["kubectl", "exec", "-n", "axiom", pod, "--", "psql", "-U", "axiom", "-d", "axiom_db",
         "-A", "-t", "-F", "|", "-f", "/tmp/_battery.sql"],
        capture_output=True, text=True, timeout=900).stdout.strip()


def embed(text: str, url: str, model: str) -> list[float]:
    req = urllib.request.Request(
        url, data=json.dumps({"model": model, "prompt": text}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["embedding"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pod", required=True, help="postgres pod name in namespace axiom")
    ap.add_argument("--embed-url", default="http://localhost:11434/api/embeddings")
    ap.add_argument("--model", default="nomic-embed-text")
    ap.add_argument("--probes", type=int, default=72)
    ap.add_argument("--structure-only", action="store_true")
    args = ap.parse_args(argv)

    print("=" * 74)
    print("STRUCTURE — what is in the index, and what can never be reached")
    print("=" * 74)
    rows = [ln for ln in psql(args.pod, STRUCTURE_SQL).splitlines() if "|" in ln]
    for line in rows:
        k, v = line.split("|", 1)
        print(f"  {k:<38} {v}")
    if not rows:
        print("  NO ROWS — kubectl could not reach the pod from here.")
        print("  This script runs node-side; an empty report is not a healthy corpus.")
        return 2
    if args.structure_only:
        return 0

    from axiom.rag.fts_query import fts_tsquery

    print()
    print("=" * 74)
    print("RETRIEVAL — real questions, both arms, the store's own query builder")
    print("=" * 74)
    failures = []
    for i, q in enumerate(DEFAULT_QUERIES, 1):
        vec = "[" + ",".join(f"{x:.6f}" for x in embed(q, args.embed_url, args.model)) + "]"
        tsq = fts_tsquery(q).replace("'", "''")
        sql = f"""SET ivfflat.probes = {args.probes};
SELECT 'dense', round(s::numeric,3), right(source_path,42) FROM (
  SELECT source_path, 1 - (embedding <=> '{vec}'::halfvec) AS s FROM chunks
  WHERE embedding IS NOT NULL AND access_tier='public' AND classification='unclassified'
  ORDER BY embedding <=> '{vec}'::halfvec LIMIT 2) a
UNION ALL
SELECT 'sparse', round(r::numeric,3), right(source_path,42) FROM (
  SELECT source_path, ts_rank(to_tsvector('english', chunk_text),
                              to_tsquery('english','{tsq}')) AS r
  FROM chunks WHERE to_tsvector('english', chunk_text) @@ to_tsquery('english','{tsq}')
    AND access_tier='public' AND classification='unclassified'
  ORDER BY r DESC LIMIT 2) b ORDER BY 1,2 DESC;"""
        rows = [r.split("|") for r in psql(args.pod, sql).splitlines() if "|" in r]
        arms = {r[0] for r in rows}
        print(f"\nQ{i}. {q}")
        for r in rows:
            print(f"   {r[0]:<7} {r[1]:>6}  {r[2]}")
        for arm in ("dense", "sparse"):
            if arm not in arms:
                print(f"   {arm:<7}  NO RESULTS")
                failures.append((i, arm))

    print()
    if failures:
        print(f"FAIL: {len(failures)} arm(s) returned nothing: {failures}", file=sys.stderr)
        return 1
    print(f"OK: both arms answered all {len(DEFAULT_QUERIES)} questions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
