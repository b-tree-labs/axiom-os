#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Did a storage change move what the index returns?

Takes two snapshots of top-k neighbours for the same query chunks — one before
a migration, one after — and reports how much of each neighbourhood survived.

**Through the index, not around it.** An exact KNN comparison measures the
encoding; this measures what a user gets, which is the encoding *and* whatever
the index does with it. When an ivfflat is rebuilt the cluster assignment
changes even if every vector is identical, so some movement is expected and
a perfect score is the surprising outcome, not the reassuring one.

    python knn_overlap.py --before knn-before.b64 --after knn-after.b64

Each input is JSON (or base64 of it) shaped `[{"qid":…,"nid":…,"rk":…}]`.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from collections import defaultdict


def load(path: str) -> dict[int, list[int]]:
    raw = open(path).read().strip()
    if not raw.lstrip().startswith("["):
        raw = base64.b64decode(raw).decode()
    out: dict[int, list[int]] = defaultdict(list)
    for r in json.loads(raw):
        out[int(r["qid"])].append(int(r["nid"]))
    return dict(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--before", required=True)
    ap.add_argument("--after", required=True)
    ap.add_argument("--min-overlap", type=float, default=None,
                    help="exit non-zero if mean overlap falls below this")
    args = ap.parse_args(argv)

    before, after = load(args.before), load(args.after)
    shared = sorted(set(before) & set(after))
    if not shared:
        print("no queries in common — the snapshots do not describe the same "
              "probes, so there is nothing to compare", file=sys.stderr)
        return 2

    missing = (set(before) | set(after)) - set(shared)
    overlaps, top1_same = [], 0
    for q in shared:
        b, a = before[q], after[q]
        overlaps.append(len(set(b) & set(a)) / max(len(b), 1))
        if b and a and b[0] == a[0]:
            top1_same += 1

    mean = sum(overlaps) / len(overlaps)
    unchanged = sum(1 for o in overlaps if o == 1.0)
    print(f"queries compared      : {len(shared)}"
          + (f"  ({len(missing)} present in only one snapshot)" if missing else ""))
    print(f"mean top-k overlap    : {mean:.4f}")
    print(f"neighbourhoods intact : {unchanged}/{len(shared)}")
    print(f"same nearest neighbour: {top1_same}/{len(shared)}")
    worst = min(overlaps)
    print(f"worst single query    : {worst:.2f}")

    if args.min_overlap is not None and mean < args.min_overlap:
        print(f"\nFAIL: mean overlap {mean:.4f} is below the {args.min_overlap} "
              f"floor this migration was allowed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
