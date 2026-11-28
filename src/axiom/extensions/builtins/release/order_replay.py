# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Shadow mode for the test order (#2441 item 6).

The ranker is off until this says it gains. For every recorded run that had
failures, this ranks that run's tests the way the ranker would have, trained only
on the runs before it, and reports against the order the run really used:

- **time to first failure**: the time until the first failing test finishes, with
  the tests started in that order on one worker, and on four (list scheduling);
- **APFD**, the average percentage of faults detected: how early the failures sit
  in the order. 1.0 is every failure first, 0.5 is no better than chance.

Both orders are scored by the same model on the same recorded durations, so the
comparison is like for like. The recorded order is the order tests finished,
which under xdist is close to, not exactly, the collection order.

    python -m axiom.extensions.builtins.release.order_replay replay "$RUNNER_TEMP/ci-history/runs.jsonl.gz"
    python -m axiom.extensions.builtins.release.order_replay backfill manifest.json --out runs.jsonl.gz

``backfill`` builds a history from the JUnit artifacts CI still retains, so the
ranker has something to learn from on day one. Its manifest is a JSON list of
``{run, zip, sha, branch, event, at, changed}``.
"""

from __future__ import annotations

import argparse
import heapq
import json
import statistics
import sys
import zipfile
import zlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from axiom.extensions.builtins.release.order_history import (
    RunRecord,
    read_history,
    run_from_junit,
    write_history,
)
from axiom.extensions.builtins.release.order_rank import MODES, RankConfig, order_ids
from axiom.infra.cli_format import Column, table

PARALLEL = 4


def apfd(order: Sequence[str], failing: set[str]) -> float:
    """Average percentage of faults detected. ``1 - sum(positions)/(n*m) + 1/(2n)``."""
    n, m = len(order), len(failing & set(order))
    if n == 0 or m == 0:
        return 1.0
    positions = sum(i + 1 for i, name in enumerate(order) if name in failing)
    return 1.0 - positions / (n * m) + 1.0 / (2 * n)


def first_failure_time(
    order: Sequence[str], seconds: dict[str, float], failing: set[str], workers: int
) -> float:
    """When the first failing test finishes: tests start in ``order`` on the next free worker."""
    free = [0.0] * max(1, workers)
    heapq.heapify(free)
    earliest = float("inf")
    for name in order:
        start = heapq.heappop(free)
        end = start + seconds[name]
        heapq.heappush(free, end)
        if name in failing and end < earliest:
            earliest = end
    return earliest


@dataclass(frozen=True)
class Measure:
    ttff: float
    ttff_parallel: float
    apfd: float


@dataclass
class RunReplay:
    run: str
    branch: str
    tests: int
    failures: int
    failures_seen_before: int
    old: Measure
    modes: dict[str, Measure] = field(default_factory=dict)


def _measure(order: Sequence[str], seconds: dict[str, float], failing: set[str]) -> Measure:
    return Measure(
        first_failure_time(order, seconds, failing, 1),
        first_failure_time(order, seconds, failing, PARALLEL),
        apfd(order, failing),
    )


def replay(
    runs: Sequence[RunRecord],
    modes: Iterable[str] = ("failed-first", "ranked"),
    min_history: int = 5,
    config: RankConfig | None = None,
    min_tests: int = 10,
) -> list[RunReplay]:
    """Score every complete run that had a failure, using only the runs before it.

    A run of fewer than ``min_tests`` tests has no order worth scoring.
    """
    rows: list[RunReplay] = []
    for index, record in enumerate(runs):
        if index < min_history or not record.complete or len(record.tests) < min_tests:
            continue
        failing = {name for name, outcome, _s in record.tests if outcome == "f"}
        if not failing:
            continue
        ids = [name for name, _o, _s in record.tests]
        seconds = {name: s for name, _o, s in record.tests}
        before = runs[:index]
        failed_before = {name for r in before for name, o, _s in r.tests if o == "f"}
        row = RunReplay(
            run=record.run,
            branch=record.branch,
            tests=len(ids),
            failures=len(failing),
            failures_seen_before=len(failing & failed_before),
            old=_measure(ids, seconds, failing),
        )
        for mode in modes:
            ordered = order_ids(ids, before, record.prefixes, mode, config)
            row.modes[mode] = _measure(ordered, seconds, failing)
        rows.append(row)
    return rows


@dataclass(frozen=True)
class Aggregate:
    mean_ttff: float
    median_ttff: float
    mean_ttff_parallel: float
    mean_apfd: float


@dataclass
class Summary:
    failing_runs: int
    failures: int
    failures_seen_before: int
    old: Aggregate
    modes: dict[str, Aggregate]
    gain: bool
    reason: str
    #: Runs where the judged mode found the first failure sooner, and later.
    wins: int = 0
    losses: int = 0


def _aggregate(measures: list[Measure]) -> Aggregate:
    if not measures:
        return Aggregate(0.0, 0.0, 0.0, 0.0)
    return Aggregate(
        statistics.fmean(m.ttff for m in measures),
        statistics.median(m.ttff for m in measures),
        statistics.fmean(m.ttff_parallel for m in measures),
        statistics.fmean(m.apfd for m in measures),
    )


def summarize(
    rows: Sequence[RunReplay], min_failing_runs: int = 20, judged_mode: str = "ranked"
) -> Summary:
    """The aggregates, and whether ``judged_mode`` earned the right to go live."""
    modes = sorted({mode for row in rows for mode in row.modes}, key=MODES.index)
    old = _aggregate([row.old for row in rows])
    by_mode = {mode: _aggregate([row.modes[mode] for row in rows]) for mode in modes}
    seen = sum(row.failures_seen_before for row in rows)
    failures = sum(row.failures for row in rows)
    count = len(rows)

    wins = sum(
        1 for row in rows if judged_mode in row.modes and row.modes[judged_mode].ttff < row.old.ttff
    )
    losses = sum(
        1 for row in rows if judged_mode in row.modes and row.modes[judged_mode].ttff > row.old.ttff
    )

    def done(gain: bool, reason: str) -> Summary:
        return Summary(count, failures, seen, old, by_mode, gain, reason, wins, losses)

    if not rows:
        return done(False, "no failing run in the history to judge against")
    if count < min_failing_runs:
        word = "run" if count == 1 else "runs"
        return done(
            False,
            f"{count} failing {word} is too few: at least {min_failing_runs} are needed to call a gain",
        )
    judged = by_mode.get(judged_mode)
    if judged is None:
        return done(False, f"{judged_mode} was not replayed")
    faster = judged.mean_ttff < old.mean_ttff
    earlier = judged.mean_apfd > old.mean_apfd
    figures = (
        f"mean time to first failure {judged.mean_ttff:.0f} s against {old.mean_ttff:.0f} s, "
        f"APFD {judged.mean_apfd:.3f} against {old.mean_apfd:.3f}, "
        f"sooner in {wins} of {count} runs and later in {losses}"
    )
    if faster and earlier and wins > losses:
        return done(
            True, f"{judged_mode} order finds failures sooner and ranks them earlier: {figures}"
        )
    return done(
        False, f"{judged_mode} order does not beat the old order on every measure: {figures}"
    )


def _aggregate_row(label: str, a: Aggregate) -> list[str]:
    return [
        label,
        f"{a.mean_ttff:.1f} s",
        f"{a.median_ttff:.1f} s",
        f"{a.mean_ttff_parallel:.1f} s",
        f"{a.mean_apfd:.3f}",
    ]


def report(
    summary: Summary, recorded_runs: int, min_history: int, rows: Sequence[RunReplay] = ()
) -> str:
    word = "run" if summary.failing_runs == 1 else "runs"
    measures = table(
        [
            _aggregate_row("old order", summary.old),
            *(_aggregate_row(mode, aggregate) for mode, aggregate in summary.modes.items()),
        ],
        [
            Column(""),
            Column("time to first failure, mean", wrap=True, align="right"),
            Column("median", align="right"),
            Column(f"mean, {PARALLEL} workers", align="right"),
            Column("APFD", align="right"),
        ],
        headers=True,
    )
    per_run = table(
        [
            [
                f"run {row.run}",
                row.branch[:22],
                row.tests,
                f"{row.failures} ({row.failures_seen_before} seen before)",
                f"{row.old.ttff:.1f} s",
                f"{row.modes['ranked'].ttff:.1f} s" if "ranked" in row.modes else "",
            ]
            for row in rows
        ],
        [
            Column(""),
            Column("", wrap=True),
            Column("tests", align="right"),
            Column("failed", wrap=True),
            Column("first failure, old order", wrap=True, align="right"),
            Column("ranked", align="right"),
        ],
        headers=True,
    )
    lines = [
        f"Shadow replay of the test order: {summary.failing_runs} failing {word} scored "
        f"of {recorded_runs} recorded (the first {min_history} only train)",
        f"failures scored: {summary.failures}; {summary.failures_seen_before} had failed before in "
        "the history, the rest are new failures no order can predict",
        "",
        *measures,
        "",
        *([*per_run, ""] if rows else []),
        f"VERDICT: {'gain' if summary.gain else 'not proven'}: {summary.reason}",
        "The ranker stays off unless AXIOM_TEST_ORDER=ranked is set explicitly.",
    ]
    return "\n".join(lines)


def _replay_command(args: argparse.Namespace) -> int:
    runs = read_history(args.history)
    if not runs:
        print(f"no history at {args.history}: nothing to replay")
        return 0
    rows = replay(runs, min_history=args.min_history)
    summary = summarize(rows, args.min_failing_runs)
    if args.json:
        print(
            json.dumps(
                {
                    "recorded_runs": len(runs),
                    "failing_runs": summary.failing_runs,
                    "failures": summary.failures,
                    "failures_seen_before": summary.failures_seen_before,
                    "old": summary.old.__dict__,
                    "modes": {mode: a.__dict__ for mode, a in summary.modes.items()},
                    "gain": summary.gain,
                    "reason": summary.reason,
                },
                indent=2,
            )
        )
    else:
        print(report(summary, len(runs), args.min_history, rows))
    return 0


def sample(
    runs: Sequence[RunRecord], per_hundred: int = 3, keep_modules: Iterable[str] = ()
) -> list[RunRecord]:
    """A small, anonymous copy of a history that replays like the original.

    Whole modules are kept (so a module's tests stay together), chosen by a stable
    hash of the module name, plus every module that held a failure, so the failures
    the replay scores are all still there. Durations go to 10 ms steps and a branch
    name becomes ``main`` or ``pr``.
    """
    keep = set(keep_modules)
    for record in runs:
        keep.update(name.partition("::")[0] for name, outcome, _s in record.tests if outcome == "f")

    def kept(module: str) -> bool:
        return module in keep or zlib.crc32(module.encode()) % 100 < per_hundred

    return [
        RunRecord(
            run=record.run,
            sha=record.sha,
            branch="main" if record.branch == "main" else "pr",
            event=record.event,
            at=record.at,
            prefixes=record.prefixes,
            tests=[
                (name, outcome, round(seconds, 2))
                for name, outcome, seconds in record.tests
                if kept(name.partition("::")[0])
            ],
            complete=record.complete,
        )
        for record in runs
    ]


def _sample_command(args: argparse.Namespace) -> int:
    runs = read_history(args.history)
    small = sample(runs, args.per_hundred)
    write_history(args.out, small, keep_runs=max(len(small), 1))
    print(f"wrote {len(small)} runs, {sum(len(r.tests) for r in small)} rows, to {args.out}")
    return 0


def _backfill_command(args: argparse.Namespace) -> int:
    manifest = Path(args.manifest)
    root = Path(args.root)
    records: list[RunRecord] = []
    for entry in sorted(json.loads(manifest.read_text()), key=lambda item: item["at"]):
        archive = Path(entry["zip"])
        if not archive.is_absolute():
            archive = manifest.parent / archive
        try:
            xml = zipfile.ZipFile(archive).read("junit.xml").decode("utf-8")
        except (OSError, KeyError, zipfile.BadZipFile):
            print(
                f"skipped run {entry['run']}: no readable junit.xml in {archive}", file=sys.stderr
            )
            continue
        records.append(
            run_from_junit(
                xml,
                run=str(entry["run"]),
                sha=entry["sha"],
                branch=entry["branch"],
                event=entry["event"],
                at=entry["at"],
                changed_paths=entry["changed"],
                exists=lambda path: (root / path).is_file(),
            )
        )
    write_history(args.out, records, keep_runs=args.keep)
    print(f"wrote {min(len(records), args.keep)} runs to {args.out}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("replay", help="score the ranker against the recorded history")
    one.add_argument("history")
    one.add_argument("--min-history", type=int, default=5)
    one.add_argument("--min-failing-runs", type=int, default=20)
    one.add_argument("--json", action="store_true")
    one.set_defaults(run=_replay_command)
    two = commands.add_parser("backfill", help="build a history from retained JUnit artifacts")
    two.add_argument("manifest")
    two.add_argument("--out", required=True)
    two.add_argument("--root", default=".", help="the checkout, to tell packages from classes")
    two.add_argument("--keep", type=int, default=200)
    two.set_defaults(run=_backfill_command)
    three = commands.add_parser("sample", help="a small anonymous copy of a history, for a fixture")
    three.add_argument("history")
    three.add_argument("--out", required=True)
    three.add_argument("--per-hundred", type=int, default=3)
    three.set_defaults(run=_sample_command)
    args = parser.parse_args(argv)
    return int(args.run(args))


if __name__ == "__main__":
    sys.exit(main())
