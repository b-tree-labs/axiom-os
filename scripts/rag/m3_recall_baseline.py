#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""M3: capture a recall snapshot the encoding change will be judged against.

Builds probes from the corpus itself (a fragment of a chunk's text, expecting
that chunk's document back), embeds each, retrieves top-k, and writes the
result. Run once before the encoding change and once after; feed both to
`m3_recall_compare.py`.

Read-only throughout. The measurement half of this program was never gated on
write access, which is why a baseline can be taken today.

**Probe selection is deterministic**, ordered by a hash of the chunk id rather
than by id itself: sequential ids would sample only the oldest chunks, and a
random sample would ask different questions before and after, which measures the
question set instead of the encoding.

The probes are written into the snapshot alongside the results, so the after run
reuses the exact same queries rather than rebuilding them and hoping they match.

    python m3_recall_baseline.py --n 60 --k 5 --out before.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

SAMPLE_SQL = """
SELECT id, source_path, chunk_text
FROM {schema}.{table}
WHERE chunk_text IS NOT NULL AND length(chunk_text) > 200
ORDER BY md5(id::text)
LIMIT %s
"""

SEARCH_SQL = """
SELECT source_path
FROM {schema}.{table}
ORDER BY embedding <=> %s::vector
LIMIT %s
"""


def embed(text: str, *, endpoint: str, model: str) -> list[float]:
    """One embedding from an Ollama-compatible endpoint."""
    payload = json.dumps({"model": model, "prompt": text}).encode()
    req = urllib.request.Request(
        f"{endpoint.rstrip('/')}/api/embeddings", data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)["embedding"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=60, help="probes (floor is 30)")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL", ""))
    ap.add_argument("--endpoint", default=os.environ.get("EMBED_ENDPOINT",
                                                         "http://localhost:11434"))
    ap.add_argument("--model", default=os.environ.get("EMBED_MODEL", "nomic-embed-text"))
    ap.add_argument("--schema", default="public")
    ap.add_argument("--table", default="chunks")
    ap.add_argument("--fragment-words", type=int, default=12)
    ap.add_argument("--out", required=True)
    ap.add_argument("--probes-from", default="",
                    help="reuse probes from an earlier snapshot (the after run "
                         "MUST pass this, or it asks different questions)")
    args = ap.parse_args(argv)

    if not args.dsn:
        print("no DSN: pass --dsn or set DATABASE_URL", file=sys.stderr)
        return 2

    import psycopg

    from axiom.rag.recall_compare import Probe, self_retrieval_probes

    with psycopg.connect(args.dsn) as conn:
        if args.probes_from:
            saved = json.load(open(args.probes_from))
            probes = [Probe(query_text=p["query_text"], expected=p["expected"],
                            chunk_id=p["chunk_id"]) for p in saved["probes"]]
            print(f"reusing {len(probes)} probes from {args.probes_from}")
        else:
            with conn.cursor() as cur:
                cur.execute(
                    SAMPLE_SQL.format(schema=args.schema, table=args.table),
                    (args.n * 2,),  # oversample: short chunks are skipped
                )
                rows = cur.fetchall()
            probes = self_retrieval_probes(rows, fragment_words=args.fragment_words)[:args.n]
            print(f"built {len(probes)} probes from {len(rows)} sampled chunks")

        if len(probes) < 30:
            print(f"WARNING: {len(probes)} probes is below the gate floor of 30",
                  file=sys.stderr)

        results = []
        for i, probe in enumerate(probes, 1):
            vector = embed(probe.query_text, endpoint=args.endpoint, model=args.model)
            with conn.cursor() as cur:
                cur.execute(SEARCH_SQL.format(schema=args.schema, table=args.table),
                            (str(vector), args.k))
                top = [r[0] for r in cur.fetchall()]
            results.append({"chunk_id": probe.chunk_id, "expected": probe.expected,
                            "returned": top})
            if i % 20 == 0:
                print(f"  {i}/{len(probes)}")

    hits = sum(1 for r in results if r["expected"] in r["returned"][:args.k])
    snapshot = {
        "k": args.k,
        "model": args.model,
        "probes": [{"query_text": p.query_text, "expected": p.expected,
                    "chunk_id": p.chunk_id} for p in probes],
        "results": results,
        "hit_rate": round(hits / len(results), 4) if results else None,
    }
    with open(args.out, "w") as fh:
        json.dump(snapshot, fh, indent=2)
    print(f"\nwrote {args.out}")
    print(f"  probes {len(probes)}  k={args.k}  hit@k {hits}/{len(results)} "
          f"= {snapshot['hit_rate']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
