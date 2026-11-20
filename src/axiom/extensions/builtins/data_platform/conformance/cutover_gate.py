# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""When a platform move is safe to cut over: two copies agreeing for long enough.

A data platform that moves to a new home runs old and new in parallel, both
receiving the same readings. Agreement on one day proves little; the question is
whether they agree day after day, through the things that go wrong in practice.

Each day, :func:`check_day` compares the two copies of silver for that UTC day
with :func:`~.reconcile.reconcile` in dry-run mode, so nothing is filled and
nothing is resolved. The old copy is the reference: a reading only the old one
has is *missing on new*, one only the new one has is *extra on new*, and the
same reading with a different value, unit or quality is a *conflict*. Any of the
three makes the day unclean.

:func:`verdict` reads the recorded days and events against a :class:`Rule` and
answers "safe" only when every condition holds, and otherwise says which do not.
It never decides that a disagreement does not matter: a person finds the cause,
fixes it, and the count starts again.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .reconcile import reconcile

#: How many disagreeing readings a day keeps as examples for whoever investigates.
EXAMPLES = 20


@dataclass
class DayCheck:
    day: str
    clean: bool
    feeds: list[str] = field(default_factory=list)
    windows: int = 0
    missing_on_new: int = 0
    extra_on_new: int = 0
    conflicts: int = 0
    examples: list[dict[str, Any]] = field(default_factory=list)


def _feeds(side, site: str, start: str, end: str) -> set[str]:
    return {window[1] for window in side.summaries(site=site, start=start, end=end)}


def check_day(old, new, *, sites: list[str], day: str) -> DayCheck:
    """Compare one UTC day of every listed site. ``old`` and ``new`` are reconcile sides."""
    start_d = date.fromisoformat(day)
    start = f"{start_d.isoformat()}T00:00:00+00:00"
    end = f"{(start_d + timedelta(days=1)).isoformat()}T00:00:00+00:00"
    result = DayCheck(day=day, clean=True)
    feeds: set[str] = set()
    for site in sites:
        feeds |= _feeds(old, site, start, end) | _feeds(new, site, start, end)
        # The old copy is "local" and the record of truth is "upstream" only so
        # that every one-sided reading is counted in a direction: nothing is
        # written, because this is a dry run.
        report = reconcile(
            old, new, site=site, start=start, end=end, record_of_truth="upstream", dry_run=True
        )
        result.windows += report.windows_compared
        result.missing_on_new += report.filled_upstream
        result.extra_on_new += report.filled_local
        result.conflicts += report.conflicts
        for c in report.conflict_list[: EXAMPLES - len(result.examples)]:
            result.examples.append({k: str(v) for k, v in c.items()})
        if (report.filled_upstream or report.filled_local) and len(result.examples) < EXAMPLES:
            result.examples.append(
                {
                    "site": site,
                    "day": day,
                    "missing_on_new": report.filled_upstream,
                    "extra_on_new": report.filled_local,
                }
            )
    result.feeds = sorted(feeds)
    result.clean = not (result.missing_on_new or result.extra_on_new or result.conflicts)
    return result


@dataclass(frozen=True)
class Rule:
    #: Consecutive clean days, ending with the latest one, with none missing.
    min_clean_days: int = 14
    #: Of those, how many must carry at least one of ``active_feeds``.
    min_active_days: int = 5
    active_feeds: tuple[str, ...] = ()
    #: Event kinds that must happen inside the run, each with a clean day after it.
    required_events: tuple[str, ...] = ("outage", "upgrade")


@dataclass
class Verdict:
    safe: bool
    clean_streak: int
    active_days: int
    reasons: list[str]


def _streak(days: list[DayCheck]) -> list[DayCheck]:
    """The run of clean, consecutive days ending at the latest recorded day."""
    ordered = sorted(days, key=lambda d: d.day)
    run: list[DayCheck] = []
    for d in reversed(ordered):
        if not d.clean:
            break
        if run and date.fromisoformat(d.day) != date.fromisoformat(run[-1].day) - timedelta(days=1):
            break
        run.append(d)
    return list(reversed(run))


def verdict(
    days: list[DayCheck], *, rule: Rule, events: list[dict[str, Any]], history_clean: bool | None
) -> Verdict:
    run = _streak(days)
    reasons: list[str] = []
    if len(run) < rule.min_clean_days:
        reasons.append(f"{len(run)} clean consecutive day(s) so far; {rule.min_clean_days} needed")

    wanted = set(rule.active_feeds)
    active = sum(1 for d in run if wanted & set(d.feeds)) if wanted else len(run)
    if active < rule.min_active_days:
        reasons.append(
            f"{active} day(s) in the run carried {', '.join(sorted(wanted)) or 'any data'}; "
            f"{rule.min_active_days} needed"
        )

    if run:
        first, last = run[0].day, run[-1].day
        for kind in rule.required_events:
            inside = [
                e for e in events if e.get("kind") == kind and first <= e.get("day", "") < last
            ]
            if not inside:
                reasons.append(
                    f"no {kind} inside the clean run ({first} to {last}) with a clean day after it"
                )
    else:
        reasons.extend(f"no {kind} inside a clean run yet" for kind in rule.required_events)

    if history_clean is None:
        reasons.append("the copied history has not been checked")
    elif not history_clean:
        reasons.append("the copied history does not match")

    return Verdict(safe=not reasons, clean_streak=len(run), active_days=active, reasons=reasons)


class GateLedger:
    """Append-only record of daily checks and events. A re-run of a day replaces it."""

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _append(self, rec: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def _records(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def record_day(self, check: DayCheck) -> None:
        self._append({"type": "day", **asdict(check)})

    def record_event(self, kind: str, day: str, *, note: str = "") -> None:
        date.fromisoformat(day)
        self._append({"type": "event", "kind": kind, "day": day, "note": note})

    def record_history(self, clean: bool, *, note: str = "") -> None:
        """The one-off comparison of the copied history: clean, or not."""
        self._append({"type": "history", "clean": bool(clean), "note": note})

    def history_clean(self) -> bool | None:
        """The latest history result, or ``None`` if history was never checked."""
        results = [r for r in self._records() if r.get("type") == "history"]
        return bool(results[-1]["clean"]) if results else None

    def days(self) -> list[DayCheck]:
        latest: dict[str, DayCheck] = {}
        for rec in self._records():
            if rec.get("type") == "day":
                rec = {k: v for k, v in rec.items() if k != "type"}
                latest[rec["day"]] = DayCheck(**rec)
        return [latest[d] for d in sorted(latest)]

    def events(self) -> list[dict[str, Any]]:
        return [
            {k: v for k, v in r.items() if k != "type"}
            for r in self._records()
            if r.get("type") == "event"
        ]


__all__ = ["DayCheck", "GateLedger", "Rule", "Verdict", "check_day", "verdict"]
