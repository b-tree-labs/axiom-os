# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Where Actions minutes go, and which of them are waste.

Each detector is a pure function over normalised runs. Minutes here are run
wall-clock, which is close to, not equal to, billed minutes; billed totals
come from the billing reading.

Selection marker contract
-------------------------
A job that narrows its tests prints one line, anywhere in its log or job
summary::

    selection: <selected>/<total> reason=<reason>

``reason=affected`` means the selector narrowed the run. Any other reason with
``selected == total`` is a full suite. Reasons listed in
``ci_health.intended_full_reasons`` (default ``full-context``: main, merge
queue, nightly) are intended; every other full-suite reason
(``map-missing``, ``base-mismatch``, ``map-stale``, ``setup-changed``, ...) is
a fallback, and counts as waste. The last marker in a log wins.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field

from axiom.extensions.builtins.release.ci_health.config import CIHealthConfig
from axiom.extensions.builtins.release.ci_health.runs import FAILED, Run

PR_EVENTS = frozenset({"pull_request", "pull_request_target"})
RED = frozenset({"failure", "timed_out", "startup_failure"})

SELECTION_MARKER = re.compile(
    r"selection:\s*(?P<selected>\d+)\s*/\s*(?P<total>\d+)\s+reason=(?P<reason>[A-Za-z0-9_.:-]+)"
)


@dataclass
class Signal:
    name: str
    count: int = 0
    minutes: float = 0.0
    share: float = 0.0
    threshold: float | None = None
    over: bool = False
    detail: str = ""
    examples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _total(runs: list[Run]) -> float:
    return sum(run.minutes for run in runs)


def _finish(signal: Signal, denominator: float, threshold: float | None) -> Signal:
    signal.minutes = round(signal.minutes, 1)
    signal.share = round(signal.minutes / denominator, 3) if denominator > 0 else 0.0
    signal.threshold = threshold
    signal.over = threshold is not None and signal.share > threshold and signal.count > 0
    signal.examples = signal.examples[:5]
    return signal


def minutes_by(runs: list[Run], key: str) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for run in runs:
        out[str(getattr(run, key))] += run.minutes
    return {k: round(v, 1) for k, v in sorted(out.items(), key=lambda kv: -kv[1])}


def minutes_by_job(jobs: list[dict]) -> dict[str, float]:
    from axiom.extensions.builtins.release.lander.model import parse_time

    out: dict[str, float] = defaultdict(float)
    for job in jobs:
        if not job.get("started_at") or not job.get("completed_at"):
            continue
        span = (parse_time(job["completed_at"]) - parse_time(job["started_at"])).total_seconds() / 60.0
        out[str(job.get("name") or "")] += max(span, 0.0)
    return {k: round(v, 1) for k, v in sorted(out.items(), key=lambda kv: -kv[1])}


# -- update-branch -----------------------------------------------------------

def _update_merge(default_branch: str) -> re.Pattern[str]:
    base = re.escape(default_branch)
    return re.compile(rf"^Merge (?:remote-tracking )?branch '(?:origin/)?{base}' into ")


def is_update_branch_run(run: Run, default_branch: str = "main") -> bool:
    """A pull-request run whose head is the default branch merged into it."""
    return run.event in PR_EVENTS and bool(_update_merge(default_branch).match(run.message))


def update_branch_share(runs: list[Run], config: CIHealthConfig) -> Signal:
    pr_runs = [run for run in runs if run.event in PR_EVENTS]
    signal = Signal("update_branch_reruns", detail="pull-request runs started by merging the default branch in")
    for run in pr_runs:
        if is_update_branch_run(run, config.default_branch):
            signal.count += 1
            signal.minutes += run.minutes
            signal.examples.append(run.url)
    finished = _finish(signal, _total(pr_runs), config.waste_share)
    if pr_runs:
        finished.detail += f"; {finished.count} of {len(pr_runs)} pull-request runs"
    return finished


# -- selection fallback ------------------------------------------------------

@dataclass(frozen=True)
class Selection:
    selected: int
    total: int
    reason: str

    def full(self) -> bool:
        return self.total > 0 and self.selected >= self.total


def parse_selection_marker(text: str) -> Selection | None:
    found = None
    for match in SELECTION_MARKER.finditer(text or ""):
        found = match
    if found is None:
        return None
    return Selection(int(found["selected"]), int(found["total"]), found["reason"])


def is_fallback(selection: Selection | None, config: CIHealthConfig) -> bool:
    if selection is None:
        return False
    if selection.reason == "affected":
        return False
    return selection.full() and selection.reason not in set(config.intended_full_reasons)


def selection_fallbacks(samples: list[tuple[Run, str]], config: CIHealthConfig, window_minutes: float) -> Signal:
    """``samples`` pairs a run with the text of its selection job's log."""
    signal = Signal("selection_fallback", detail="pull-request runs where test selection ran the full suite")
    reasons: dict[str, int] = defaultdict(int)
    unmarked = 0
    for run, text in samples:
        selection = parse_selection_marker(text)
        if selection is None:
            unmarked += 1
            continue
        if is_fallback(selection, config):
            signal.count += 1
            signal.minutes += run.minutes
            reasons[selection.reason] += 1
            signal.examples.append(run.url)
    parts = [f"{len(samples)} sampled"]
    if reasons:
        parts.append("reasons " + ", ".join(f"{k}={v}" for k, v in sorted(reasons.items())))
    if unmarked:
        parts.append(f"{unmarked} without a selection marker")
    finished = _finish(signal, sum(run.minutes for run, _ in samples) or window_minutes, config.waste_share)
    finished.detail += "; " + "; ".join(parts)
    return finished


# -- flakes, chronic red, duplicates, failed minutes -------------------------

def flakes(runs: list[Run], config: CIHealthConfig) -> Signal:
    """Same head, failed then passed: a re-run attempt or a second run."""
    signal = Signal("same_sha_flakes", detail="heads that failed and then passed with no new commit")
    seen: set[tuple[int, str]] = set()
    by_head: dict[tuple[int, str], list[Run]] = defaultdict(list)
    for run in runs:
        by_head[(run.workflow_id, run.head_sha)].append(run)
        if run.run_attempt > 1 and run.conclusion == "success":
            seen.add((run.workflow_id, run.head_sha))
            signal.count += 1
            signal.minutes += run.minutes * (run.run_attempt - 1) / run.run_attempt
            signal.examples.append(run.url)
    for key, group in by_head.items():
        if key in seen or len(group) < 2:
            continue
        ordered = sorted(group, key=lambda r: r.created_at)
        failed = [r for r in ordered if r.conclusion in RED]
        if failed and any(r.conclusion == "success" and r.created_at > failed[0].created_at for r in ordered):
            signal.count += 1
            signal.minutes += sum(r.minutes for r in failed)
            signal.examples.append(failed[0].url)
    return _finish(signal, _total(runs), config.waste_share)


def chronic_red(runs: list[Run], config: CIHealthConfig) -> Signal:
    """Branches whose last N completed runs of one workflow all failed."""
    signal = Signal("chronic_red", detail=f"branches red on their last {config.chronic_red_runs} runs")
    groups: dict[tuple[str, str], list[Run]] = defaultdict(list)
    for run in runs:
        if run.status == "completed" and run.conclusion and run.conclusion != "cancelled":
            groups[(run.workflow, run.head_branch)].append(run)
    branches = []
    for (workflow, branch), group in groups.items():
        recent = sorted(group, key=lambda r: r.created_at, reverse=True)[: config.chronic_red_runs]
        if len(recent) == config.chronic_red_runs and all(r.conclusion in RED for r in recent):
            signal.count += 1
            signal.minutes += sum(r.minutes for r in recent)
            branches.append(f"{branch} ({workflow})")
            signal.examples.append(recent[0].url)
    finished = _finish(signal, _total(runs), None)
    finished.over = finished.count > 0
    if branches:
        finished.detail += ": " + ", ".join(sorted(branches)[:5])
    return finished


def duplicate_main_runs(runs: list[Run], config: CIHealthConfig) -> Signal:
    signal = Signal("duplicate_main_runs", detail="more than one run of a workflow for one default-branch head")
    groups: dict[tuple[int, str], list[Run]] = defaultdict(list)
    for run in runs:
        if run.head_branch == config.default_branch and run.event in {"push", "merge_group"}:
            groups[(run.workflow_id, run.head_sha)].append(run)
    for group in groups.values():
        if len(group) > 1:
            extra = sorted(group, key=lambda r: r.created_at)[1:]
            signal.count += len(extra)
            signal.minutes += sum(r.minutes for r in extra)
            signal.examples.extend(r.url for r in extra)
    return _finish(signal, _total(runs), config.waste_share)


def main_push_share(runs: list[Run], config: CIHealthConfig) -> Signal:
    signal = Signal("main_push_suite", detail="minutes spent re-testing default-branch pushes")
    for run in runs:
        if run.event == "push" and run.head_branch == config.default_branch:
            signal.count += 1
            signal.minutes += run.minutes
    return _finish(signal, _total(runs), config.waste_share)


def failed_minutes(runs: list[Run], config: CIHealthConfig) -> Signal:
    signal = Signal("failed_or_cancelled", detail="minutes on runs that failed, timed out, or were cancelled")
    for run in runs:
        if run.status == "completed" and run.conclusion in FAILED:
            signal.count += 1
            signal.minutes += run.minutes
    return _finish(signal, _total(runs), config.waste_share)


def superseded_signal(decisions, runs: list[Run], config: CIHealthConfig) -> Signal:
    signal = Signal("superseded_running", detail="runs still going after a newer head started")
    for decision in decisions:
        if decision.cancel:
            signal.count += 1
            signal.minutes += decision.run.minutes
            signal.examples.append(decision.run.url)
    finished = _finish(signal, _total(runs), None)
    finished.over = finished.count > 0
    return finished
