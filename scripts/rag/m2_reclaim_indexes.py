#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""M2: drop the duplicate index, and prove the reclaim.

Takes a before measurement, drops indexes whose expression duplicates another,
takes an after measurement, and reports the comparison. The proof is part of the
operation rather than something to remember afterwards.

**It only ever drops a duplicate.** A never-scanned index is reported and left
alone: it may serve a feature that has not shipped, and the scan statistic
resets when statistics do. A human decides those.

**It picks the less-used member of a duplicate pair**, so the planner keeps the
index it has been choosing.

Dry run by default. `--apply` performs the drop, and needs a role with DROP
privilege on the schema — the monitor role used for measurement cannot.

    python m2_reclaim_indexes.py                 # report only
    python m2_reclaim_indexes.py --apply         # drop, with before/after
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="perform the drop (needs DROP privilege)")
    parser.add_argument("--dsn", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--schema", default="public")
    parser.add_argument("--table", default="chunks")
    args = parser.parse_args(argv)

    if not args.dsn:
        print("no DSN: pass --dsn or set DATABASE_URL", file=sys.stderr)
        return 2

    import psycopg

    from axiom.rag.storage_stat import (
        collect,
        compare,
        duplicate_expressions,
        never_scanned,
        render,
    )

    with psycopg.connect(args.dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            before = collect(cur, schema=args.schema, table=args.table)

        print("=== before ===")
        for line in render(before):
            print(line)

        groups = duplicate_expressions(before.indexes)
        by_name = {i.name: i for i in before.indexes}
        # Keep the member the planner actually uses; drop the other.
        doomed = []
        for group in groups:
            ordered = sorted(group, key=lambda n: by_name[n].scans, reverse=True)
            doomed.extend(ordered[1:])

        cold = never_scanned(before.indexes)
        if cold:
            print("\n  never scanned, NOT dropped (a human decides these):")
            for index in cold:
                print(f"    {index.name}  {index.size_bytes / 1e6:.0f} MB  "
                      f"{index.definition[:70]}")

        if not doomed:
            print("\nno duplicate expressions: nothing to reclaim")
            return 0

        reclaim = sum(by_name[n].size_bytes for n in doomed)
        print(f"\n  duplicates to drop: {', '.join(doomed)}  "
              f"({reclaim / 1e9:.2f} GB)")

        if not args.apply:
            print("\ndry run. re-run with --apply to drop.")
            return 0

        for name in doomed:
            # CONCURRENTLY so readers are never blocked. It cannot run inside a
            # transaction, hence autocommit above. A failure leaves an INVALID
            # index behind, which is reported rather than retried blindly.
            statement = f'DROP INDEX CONCURRENTLY IF EXISTS "{args.schema}"."{name}"'
            print(f"  {statement}")
            with conn.cursor() as cur:
                cur.execute(statement)

        with conn.cursor() as cur:
            after = collect(cur, schema=args.schema, table=args.table)

    print("\n=== after ===")
    for line in render(after):
        print(line)

    result = compare(before, after)
    print("\n=== proof ===")
    print(json.dumps({k: result[k] for k in
                      ("total_bytes", "other_index_bytes", "bytes_per_chunk",
                       "confounded", "improved", "note")}, indent=2))
    if result["confounded"]:
        print("\nCOMPARISON CONFOUNDED — the reclaim is not proven by these numbers.")
        return 1
    if not result["improved"]:
        print("\nNO REDUCTION MEASURED — investigate before claiming M2.")
        return 1
    print(f"\nM2 proven: {-result['total_bytes']['delta'] / 1e9:.2f} GB reclaimed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
