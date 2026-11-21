#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The nightly scale job's verdict (LS-1, #1780): measured numbers against the budgets file.

    python scripts/ls1_scale_report.py --numbers _scale/numbers.json \
        [--budgets tests/logbook_acceptance/ls1_budgets.toml] [--summary FILE] [--update-baseline]

Reads the numbers the scale tests recorded (``{"rows": n, "numbers": {key: ms}}``) and the budgets
file, prints one summary line and a Markdown table (appended to ``--summary``, the job's
``$GITHUB_STEP_SUMMARY``), and exits 1 when an enforced number is at or over its limit, an
enforced or tracked number regressed more than the file's ``regression`` (20%) past its stored
baseline, or an enforced budget recorded no number at all (its test did not run). Pending budgets
are listed, never failed. ``--update-baseline`` writes the measured numbers into the file's
``baseline`` fields, for a pull request that raises them. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BUDGETS = Path(__file__).resolve().parents[1] / "tests" / "logbook_acceptance" / "ls1_budgets.toml"


@dataclass(frozen=True)
class Verdict:
    key: str
    status: str
    what: str
    value: float | None
    limit: float | None
    baseline: float | None
    verdict: str  # ok | over | regressed | missing | pending | no-baseline
    note: str = ""

    @property
    def failed(self) -> bool:
        return self.verdict in ("over", "regressed", "missing")


def load_budgets(path: Path = DEFAULT_BUDGETS) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def evaluate(budgets: dict, numbers: dict[str, float]) -> list[Verdict]:
    threshold = float(budgets.get("regression", 0.20))
    noise = float(budgets.get("noise_ms", 0.0))
    out = []
    for key, b in budgets["budget"].items():
        status, what = b.get("status", "enforced"), b.get("what", "")
        limit = b.get("limit")
        base = float(b.get("baseline") or 0.0) or None
        value = numbers.get(key)
        if status == "pending":
            out.append(Verdict(key, status, what, value, limit, base, "pending", b.get("needs", "")))
            continue
        if value is None:
            verdict = "missing" if status == "enforced" else "pending"
            out.append(Verdict(key, status, what, None, limit, base, verdict, "no number recorded"))
            continue
        if status == "enforced" and limit is not None and value >= float(limit):
            out.append(Verdict(key, status, what, value, limit, base, "over",
                               f"{value:.0f} ms at or over {float(limit):.0f} ms"))  # fmt: skip
            continue
        if base is not None and value > base * (1 + threshold) and value - base > noise:
            pct = 100.0 * (value - base) / base
            out.append(Verdict(key, status, what, value, limit, base, "regressed",
                               f"{pct:+.0f}% against the baseline {base:.1f} ms"))  # fmt: skip
            continue
        out.append(Verdict(key, status, what, value, limit, base, "ok" if base else "no-baseline"))
    return out


def summary_line(verdicts: list[Verdict], rows: int | None) -> str:
    failed = [v for v in verdicts if v.failed]
    measured = [v for v in verdicts if v.value is not None]
    head = f"LS-1 at {rows:,} entries: " if rows else "LS-1: "
    if failed:
        return head + f"{len(failed)} failed ({', '.join(f'{v.key} {v.verdict}' for v in failed)})"
    worst = max(measured, key=lambda v: (v.value or 0) / float(v.limit or 1e9), default=None)
    tail = f"; nearest its limit: {worst.key} {worst.value:.0f}/{worst.limit:.0f} ms" if worst and worst.limit else ""
    pending = sum(v.verdict == "pending" for v in verdicts)
    return head + f"{len(measured)} measured, all within budget{tail}; {pending} pending"


def markdown(verdicts: list[Verdict], rows: int | None) -> str:
    def ms(x: float | None) -> str:
        return "" if x is None else f"{x:.1f}"

    lines = [f"### {summary_line(verdicts, rows)}", "",
             "| budget | what | ms | limit | baseline | verdict |", "|---|---|---:|---:|---:|---|"]  # fmt: skip
    for v in verdicts:
        verdict = v.verdict + (f": {v.note}" if v.note else "")
        lines.append(f"| `{v.key}` | {v.what} | {ms(v.value)} | {ms(v.limit)} | {ms(v.baseline)} | {verdict} |")
    return "\n".join(lines) + "\n"


def update_baseline(path: Path, numbers: dict[str, float]) -> int:
    """Write each measured number into its ``baseline`` line, in place; returns how many."""
    text, key, changed = path.read_text(encoding="utf-8").splitlines(keepends=True), None, 0
    for i, line in enumerate(text):
        m = re.match(r"\[budget\.([A-Za-z0-9_]+)\]", line)
        if m:
            key = m.group(1)
        elif key in numbers and re.match(r"baseline\s*=", line):
            text[i] = f"baseline = {numbers[key]:.1f}\n"
            changed += 1
    path.write_text("".join(text), encoding="utf-8")
    return changed


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--numbers", type=Path, required=True)
    p.add_argument("--budgets", type=Path, default=DEFAULT_BUDGETS)
    p.add_argument("--summary", type=Path, help="append the Markdown table here")
    p.add_argument("--update-baseline", action="store_true")
    args = p.parse_args(argv)
    budgets = load_budgets(args.budgets)
    try:
        report = json.loads(args.numbers.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"LS-1: no numbers to judge ({exc}); the scale tests did not record any", file=sys.stderr)
        return 1
    numbers = {k: float(v) for k, v in (report.get("numbers") or {}).items()}
    if args.update_baseline:
        print(f"updated {update_baseline(args.budgets, numbers)} baselines in {args.budgets}")
        return 0
    verdicts = evaluate(budgets, numbers)
    print(summary_line(verdicts, report.get("rows")))
    table = markdown(verdicts, report.get("rows"))
    print(table)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as fh:
            fh.write(table)
    return 1 if any(v.failed for v in verdicts) else 0


if __name__ == "__main__":
    sys.exit(main())
