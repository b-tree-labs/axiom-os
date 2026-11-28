# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Shard the full unit suite, and prove the shards held every test (#2441 item 9).

Main, the merge queue and a release run every test. One runner takes about 25
minutes; three take about a third of the test time each. The split is:

- **by module**, so a module's tests stay together and in their order;
- **balanced by recorded duration** (longest module first onto the lightest shard),
  from a committed table, ``scripts/unit_shard_durations.json``, built from the
  history by ``table``;
- **a pure function** of the module name and that table. Every shard computes the
  same assignment with no shared state to race on: a cache another run saves
  between two shards' starts cannot send a module to two shards or to none. A
  module the table does not know goes to ``crc32(name) % shards``, so a new test
  file is still run, only maybe not perfectly balanced, until the table is rebuilt.

``order_plugin`` applies it when ``AXIOM_TEST_SHARD=<i>/<n>`` is set (one-based), so
the unit step stays a bare pytest. A malformed value stops the run: a shard that
silently ran everything, or nothing, would be a bug nobody saw.

Each shard writes a report (how many tests were collected, how many it kept, and a
digest of each set). ``verify`` adds the reports up: every shard present, all of them
saw the same suite, and the counts and digests of what ran sum to the whole. A test
lost or run twice changes a digest even when the counts still add up.

    python -m axiom.extensions.builtins.release.order_shard table HISTORY --out scripts/unit_shard_durations.json
    python -m axiom.extensions.builtins.release.order_shard history --out .ci-history/runs.jsonl.gz shard-*/history-fragment.jsonl.gz
    python -m axiom.extensions.builtins.release.order_shard verify --shards 3 shard-1.json shard-2.json shard-3.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import zlib
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from axiom.extensions.builtins.release.order_history import (
    RunRecord,
    append_run,
    read_history,
)

_MASK = (1 << 64) - 1
TABLE_VERSION = 1


def _module(test_id: str) -> str:
    return test_id.partition("::")[0]


def parse_spec(spec: str) -> tuple[int, int]:
    """``"2/3"`` is shard 2 of 3, returned zero-based as ``(1, 3)``. Anything else raises."""
    parts = spec.strip().split("/")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError(f"shard {spec!r} is not <index>/<count>")
    index, count = int(parts[0]), int(parts[1])
    if count < 1 or not 1 <= index <= count:
        raise ValueError(f"shard {spec!r} is out of range: index 1..count, count at least 1")
    return index - 1, count


def assign(table: dict[str, float], count: int) -> dict[str, int]:
    """Longest module first onto the lightest shard; ties go to the lowest shard."""
    loads = [0.0] * count
    placed: dict[str, int] = {}
    for name, seconds in sorted(table.items(), key=lambda item: (-item[1], item[0])):
        target = min(range(count), key=lambda shard: (loads[shard], shard))
        placed[name] = target
        loads[target] += seconds
    return placed


def shard_of(module: str, placed: dict[str, int], count: int) -> int:
    return placed[module] if module in placed else zlib.crc32(module.encode()) % count


def select(ids: Sequence[str], index: int, count: int, table: dict[str, float]) -> list[str]:
    """The ids in shard ``index`` (zero-based), in collection order."""
    if count == 1:
        return list(ids)
    placed = assign(table, count)
    return [tid for tid in ids if shard_of(_module(tid), placed, count) == index]


def build_table(
    runs: Sequence[RunRecord], min_seconds: float = 0.5, last: int = 10
) -> dict[str, float]:
    """Each module's mean total seconds over the newest ``last`` complete runs it was in."""
    totals: dict[str, list[float]] = defaultdict(list)
    for record in [r for r in runs if r.complete][-last:]:
        per_run: dict[str, float] = defaultdict(float)
        for name, outcome, seconds in record.tests:
            if outcome != "s":
                per_run[_module(name)] += seconds
        for module, seconds in per_run.items():
            totals[module].append(seconds)
    means = {module: statistics.fmean(values) for module, values in totals.items()}
    return {module: seconds for module, seconds in means.items() if seconds >= min_seconds}


def save_table(path: Path | str, table: dict[str, float]) -> None:
    body = {"v": TABLE_VERSION, "seconds": {k: round(table[k], 2) for k in sorted(table)}}
    Path(path).write_text(json.dumps(body, indent=1) + "\n", encoding="utf-8")


def load_table(path: Path | str) -> dict[str, float]:
    """The table, or ``{}`` when it is missing or unreadable: balance is lost, no test is."""
    try:
        body = json.loads(Path(path).read_text(encoding="utf-8"))
        if body.get("v") != TABLE_VERSION:
            return {}
        return {str(k): float(v) for k, v in body["seconds"].items()}
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        return {}


def digest(ids: Iterable[str]) -> int:
    """Order-free fingerprint of a set of test ids: the sum of their crc32, mod 2**64."""
    return sum(zlib.crc32(tid.encode()) for tid in ids) & _MASK


@dataclass(frozen=True)
class ShardReport:
    index: int  # one-based
    count: int
    total: int  # tests collected, before this shard dropped any
    total_digest: int
    selected: int
    selected_digest: int


def verify(reports: Sequence[ShardReport], expected_shards: int) -> list[str]:
    """Reasons the shards do not together hold the suite exactly once. Empty means they do."""
    if not reports:
        return ["no shard reported: nothing proves any test ran"]
    problems: list[str] = []
    seen: dict[int, int] = defaultdict(int)
    for report in reports:
        seen[report.index] += 1
        if report.count != expected_shards:
            problems.append(
                f"shard {report.index} split the suite {report.count} ways, the workflow runs {expected_shards}"
            )
    for index in range(1, expected_shards + 1):
        if seen[index] == 0:
            problems.append(f"shard {index} of {expected_shards} did not report")
        elif seen[index] > 1:
            problems.append(f"shard {index} reported {seen[index]} times")
    if len({(r.total, r.total_digest) for r in reports}) > 1:
        problems.append("the shards collected different suites (their totals or digests differ)")
        return problems
    first = reports[0]
    if sum(r.selected for r in reports) != first.total:
        problems.append(
            f"the shards ran {sum(r.selected for r in reports)} tests of the {first.total} collected"
        )
    if sum(r.selected_digest for r in reports) & _MASK != first.total_digest:
        problems.append(
            "the tests the shards ran are not the tests collected: one was lost or run twice"
        )
    return problems


def join_fragments(fragments: Sequence[Path | str], out: Path | str) -> int:
    """Join the shards' one-run history files into one record appended to ``out``.

    Returns the number of tests joined. Raises ``ValueError`` on a missing or empty
    fragment: a record that lacks a shard must not be saved as if it were the run.
    """
    records: list[RunRecord] = []
    for fragment in fragments:
        runs = read_history(fragment)
        if len(runs) != 1:
            raise ValueError(
                f"{fragment} holds {len(runs)} runs; a shard fragment holds exactly one"
            )
        records.append(runs[0])
    if not records:
        raise ValueError("no fragments to join")
    first = records[0]
    joined = RunRecord(
        run=first.run,
        sha=first.sha,
        branch=first.branch,
        event=first.event,
        at=first.at,
        prefixes=first.prefixes,
        tests=[row for record in records for row in record.tests],
        complete=all(record.complete for record in records),
    )
    append_run(out, joined)
    return len(joined.tests)


def _history_command(args: argparse.Namespace) -> int:
    try:
        count = join_fragments(args.fragments, args.out)
    except ValueError as error:
        print(f"::warning::history not joined: {error}")
        return 1
    print(f"joined {len(args.fragments)} fragments, {count} tests, into {args.out}")
    return 0


def _verify_command(args: argparse.Namespace) -> int:
    reports: list[ShardReport] = []
    for name in args.reports:
        try:
            reports.append(ShardReport(**json.loads(Path(name).read_text(encoding="utf-8"))))
        except (OSError, ValueError, TypeError) as error:
            print(f"unreadable shard report {name}: {error}", file=sys.stderr)
            return 1
    problems = verify(reports, args.shards)
    for problem in problems:
        print(f"::error::{problem}")
    if problems:
        return 1
    print(
        f"every collected test ran in exactly one shard: {reports[0].total} tests, "
        f"{', '.join(str(r.selected) for r in sorted(reports, key=lambda r: r.index))} per shard"
    )
    return 0


def _table_command(args: argparse.Namespace) -> int:
    table = build_table(read_history(args.history), args.min_seconds)
    save_table(args.out, table)
    print(f"wrote {len(table)} modules ({sum(table.values()):.0f} s) to {args.out}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("verify", help="check that the shards together held every test once")
    one.add_argument("--shards", type=int, required=True)
    one.add_argument("reports", nargs="*")
    one.set_defaults(run=_verify_command)
    two = commands.add_parser("table", help="build the duration table from a history")
    two.add_argument("history")
    two.add_argument("--out", required=True)
    two.add_argument("--min-seconds", type=float, default=0.5)
    two.set_defaults(run=_table_command)
    three = commands.add_parser(
        "history", help="join the shards' history fragments into the run's record"
    )
    three.add_argument("--out", required=True)
    three.add_argument("fragments", nargs="+")
    three.set_defaults(run=_history_command)
    args = parser.parse_args(argv)
    return int(args.run(args))


if __name__ == "__main__":
    sys.exit(main())
