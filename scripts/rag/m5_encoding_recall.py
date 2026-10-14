#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Which vector encoding can this corpus afford? Measured, not assumed.

Shrinking the vector component has several levers — fewer dimensions
(Matryoshka truncation), fewer bits per dimension (`halfvec`, int8), or both —
and they are not interchangeable. On one live corpus truncation to 512
dimensions cost 15% of the retrieval neighbourhood to save a third of the
bytes, while float16 cost nothing measurable and saved half. Reasoning from the
model card would have picked the worse one.

**No embedder and no LLM.** The question is whether an encoding preserves the
neighbourhood the index is built on, and the corpus's own stored vectors answer
it: take the exact top-k at full precision as ground truth, re-encode, and
measure how much of that neighbourhood survives.

    python m5_encoding_recall.py --vectors sample.json

Input is `[{"id": ..., "v": "[0.1,0.2,...]"}]`, which is what

    SELECT json_agg(json_build_object('id', id, 'v', embedding::text))
    FROM (SELECT id, embedding FROM chunks
          WHERE embedding IS NOT NULL ORDER BY md5(id::text) LIMIT 3000) t

produces. Sampling deterministically matters: a before and an after must ask
the same questions or the comparison measures the sample.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys

DIMS = (512, 384, 256, 128, 64)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vectors", required=True,
                    help="JSON or base64-of-JSON from the query above")
    ap.add_argument("--probes", type=int, default=60)
    ap.add_argument("--k", type=int, default=10)
    args = ap.parse_args(argv)

    import numpy as np

    raw = open(args.vectors).read().strip()
    if not raw.lstrip().startswith("["):
        raw = base64.b64decode(raw).decode()
    rows = json.loads(raw)
    V = np.array([np.fromstring(r["v"].strip("[]"), sep=",") for r in rows], dtype=np.float64)
    if len(V) <= args.probes:
        print(f"need more than {args.probes} vectors, got {len(V)}", file=sys.stderr)
        return 2

    n = args.probes
    k = args.k

    def l2(X):
        return X / np.clip(np.linalg.norm(X, axis=1, keepdims=True), 1e-12, None)

    def topk(base):
        b, q = l2(base), l2(base[:n])
        sims = q @ b.T
        for i in range(n):
            sims[i, i] = -2.0          # a vector is its own nearest neighbour
        return np.argsort(-sims, axis=1)[:, :k]

    truth = topk(V)

    def recall(base) -> float:
        got = topk(base)
        return sum(len(set(got[i]) & set(truth[i])) for i in range(n)) / (n * k)

    norms = np.linalg.norm(V, axis=1)
    normalized = abs(norms.mean() - 1.0) < 0.01
    print(f"{len(V)} vectors, {V.shape[1]} dims, {n} probes, recall@{k}")
    print(f"stored L2 norm mean {norms.mean():.4f} -> "
          f"{'already normalized' if normalized else 'NOT normalized'}")
    if normalized:
        print("  note: layer_norm before truncation cannot help — normalizing at")
        print("  write time already discarded the scale it would act on.")
    print()

    d = V.shape[1]
    print(f"{'variant':<34} {'recall@' + str(k):>9}  {'bytes/vector':>12}")
    print(f"{'-' * 34} {'-' * 9}  {'-' * 12}")
    print(f"{'float32, ' + str(d) + ' dim (baseline)':<34} {1.0:>9.4f}  {d * 4:>10} B")
    print(f"{'float16 halfvec, ' + str(d) + ' dim':<34} "
          f"{recall(V.astype(np.float16).astype(np.float64)):>9.4f}  {d * 2:>10} B")
    print(f"{'int8 scalar-quantized, ' + str(d) + ' dim':<34} "
          f"{recall(np.round(np.clip(V, -1, 1) * 127) / 127):>9.4f}  {d * 1:>10} B")
    for dim in DIMS:
        if dim >= d:
            continue
        print(f"{'float32, ' + str(dim) + ' dim (Matryoshka)':<34} "
              f"{recall(V[:, :dim]):>9.4f}  {dim * 4:>10} B")
    print()
    print("Read it as a frontier: an encoding is only worth choosing if nothing")
    print("above it is both cheaper and at least as accurate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
