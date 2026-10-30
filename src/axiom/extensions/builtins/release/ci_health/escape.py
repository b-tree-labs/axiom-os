# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Balancing fast pull-request test selection against safety, by measurement.

Pull requests run a narrowed selection. The default branch runs everything.
Two readings come from comparing them:

* **Escape rate.** A main run failed, and a pull request merged since the last
  green main run had a narrowed selection (``reason=affected``) that passed.
  The failing tests were not in that selection, or the pull request changed a
  file the tests read. The rate is escaped pull requests over narrowed merges.
  When it passes ``escape_rate_warn`` the report *proposes* a lower cap or a
  new full-suite trigger. It never edits ``scripts/testmon_blast_radius.py``.
* **Red-main culprit.** The first red commit after green, its pull request,
  its author, and the failing test ids from the log. RIVET alerts once. For a
  listed author's own pull request it may open a *draft* revert. It never
  merges, and it never acts on anyone else's pull request.

Every forge call goes through ``CIClient``; tests pass fixtures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import PurePosixPath

from axiom.extensions.builtins.release.ci_health import detect
from axiom.extensions.builtins.release.ci_health.client import CIClient, CIClientError
from axiom.extensions.builtins.release.ci_health.config import CIHealthConfig
from axiom.extensions.builtins.release.ci_health.runs import Run
from axiom.extensions.builtins.release.lander.model import parse_time

_FAILED_ID = re.compile(r"(?:FAILED|ERROR)\s+([\w./\[\]-]+\.py::\S+)")
_RESULT_ID = re.compile(r"([\w./\[\]-]+\.py::\S+)\s+(?:PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)")
_PR_REF = re.compile(r"\(#(\d+)\)")
_NO_JOB = "-"
TRIGGER_FILE = "scripts/testmon_blast_radius.py"


def failing_tests(log: str) -> list[str]:
    """Test ids pytest reported as failed or errored, in first-seen order."""
    seen: dict[str, None] = {}
    for match in _FAILED_ID.finditer(log or ""):
        seen.setdefault(match.group(1).rstrip(","), None)
    return list(seen)


def selected_tests(log: str) -> set[str]:
    """Test ids a verbose log lists with a result. Empty when the log is not verbose."""
    return {m.group(1) for m in _RESULT_ID.finditer(log or "")}


def file_type(path: str) -> str:
    pure = PurePosixPath(path)
    return pure.suffix.lower() or pure.name


# -- pulls and runs ----------------------------------------------------------


@dataclass(frozen=True)
class Pull:
    number: int
    title: str
    author: str
    url: str
    node_id: str
    merged_at: datetime
    merge_sha: str
    head_sha: str

    def to_dict(self) -> dict:
        return {"number": self.number, "title": self.title, "author": self.author, "url": self.url}


def parse_pull(row: dict) -> Pull:
    user = row.get("user") if isinstance(row.get("user"), dict) else {}
    head = row.get("head") if isinstance(row.get("head"), dict) else {}
    return Pull(
        number=int(row.get("number") or 0),
        title=str(row.get("title") or ""),
        author=str((user or {}).get("login") or ""),
        url=str(row.get("html_url") or ""),
        node_id=str(row.get("node_id") or ""),
        merged_at=parse_time(row.get("merged_at")),
        merge_sha=str(row.get("merge_commit_sha") or ""),
        head_sha=str((head or {}).get("sha") or ""),
    )


def selection_workflow_names(runs: list[Run], config: CIHealthConfig) -> set[str]:
    names = set(config.selection_workflows)
    if names:
        return names
    pr_runs = [r for r in runs if r.event in detect.PR_EVENTS and r.status == "completed"]
    if not pr_runs:
        return set()
    return {next(iter(detect.minutes_by(pr_runs, "workflow")))}


def main_runs(runs: list[Run], config: CIHealthConfig, workflows: set[str]) -> list[Run]:
    """Finished default-branch push runs of the selection workflow, oldest first."""
    rows = [
        r
        for r in runs
        if r.event == "push"
        and r.head_branch == config.default_branch
        and r.workflow in workflows
        and r.status == "completed"
        and r.conclusion in {"success", "failure"}
    ]
    return sorted(rows, key=lambda r: r.created_at)


def last_pr_run(pull: Pull, runs: list[Run], workflows: set[str]) -> Run | None:
    found = [
        r
        for r in runs
        if r.event in detect.PR_EVENTS
        and r.workflow in workflows
        and r.status == "completed"
        and (r.head_sha == pull.head_sha or pull.number in r.pr_numbers)
    ]
    return max(found, key=lambda r: r.created_at) if found else None


def _selection_job(client: CIClient, run: Run, config: CIHealthConfig) -> dict | None:
    patterns = tuple(p.lower() for p in config.selection_jobs)
    for job in client.jobs(run.repo, run.id):
        if any(p in str(job.get("name") or "").lower() for p in patterns):
            return job
    return None


class _Reads:
    """Log reads one sweep may still spend. A cached marker costs nothing."""

    def __init__(self, budget: int) -> None:
        self.left = budget


def narrowed_marker(
    client: CIClient, run: Run, config: CIHealthConfig, cache: dict, reads: _Reads
) -> detect.Selection | None | bool:
    """The run's selection marker. False when it is not cached and the budget is spent."""
    key = str(run.id)
    if key not in cache:
        if reads.left <= 0:
            return False
        reads.left -= 1
        text = _NO_JOB
        try:
            job = _selection_job(client, run, config)
            if job is not None:
                found = detect.parse_selection_marker(client.job_log(run.repo, int(job.get("id") or 0)) or "")
                text = f"selection: {found.selected}/{found.total} reason={found.reason}" if found else ""
        except CIClientError:
            return False
        cache[key] = text
    text = cache[key]
    return detect.parse_selection_marker(text) if text and text != _NO_JOB else None


def is_narrowed(selection: detect.Selection | None | bool) -> bool:
    return bool(selection) and selection.reason == "affected"  # type: ignore[union-attr]


# -- escape rate --------------------------------------------------------------


@dataclass
class Escape:
    pull: Pull
    main_run: Run
    tests: list[str]
    file_types: list[str]
    why: str

    def to_dict(self) -> dict:
        return {
            "pr": self.pull.to_dict(),
            "main_run": self.main_run.id,
            "main_run_url": self.main_run.url,
            "tests": self.tests,
            "file_types": self.file_types,
            "why": self.why,
        }


@dataclass
class EscapeReport:
    window_days: int
    merges: int = 0
    narrowed_merges: int = 0
    unread_merges: int = 0
    escapes: list[Escape] = field(default_factory=list)
    rate_warn: float = 0.0
    recommendations: list[str] = field(default_factory=list)

    @property
    def rate(self) -> float | None:
        return len(self.escapes) / self.narrowed_merges if self.narrowed_merges else None

    @property
    def per_30_days(self) -> float:
        return len(self.escapes) * 30.0 / max(self.window_days, 1)

    @property
    def over(self) -> bool:
        return self.rate is not None and bool(self.escapes) and self.rate >= self.rate_warn

    def to_dict(self) -> dict:
        return {
            "window_days": self.window_days,
            "merges": self.merges,
            "narrowed_merges": self.narrowed_merges,
            "unread_merges": self.unread_merges,
            "escaped": len(self.escapes),
            "rate": None if self.rate is None else round(self.rate, 4),
            "per_30_days": round(self.per_30_days, 2),
            "rate_warn": self.rate_warn,
            "over": self.over,
            "escapes": [e.to_dict() for e in self.escapes],
            "recommendations": self.recommendations,
        }


def recommend(escapes: list[Escape], config: CIHealthConfig) -> list[str]:
    """Proposals only. Nothing here edits the selection script."""
    out: list[str] = []
    by_type: dict[str, int] = {}
    for escape in escapes:
        for kind in escape.file_types:
            if kind != ".py":
                by_type[kind] = by_type.get(kind, 0) + 1
    for kind, count in sorted(by_type.items(), key=lambda kv: (-kv[1], kv[0])):
        pattern = kind if not kind.startswith(".") else f"*{kind}"
        out.append(
            f"Add `{pattern}` to the full-suite triggers in {TRIGGER_FILE}: "
            f"{count} escaped pull request(s) changed a {kind} file the tests read."
        )
    if any(all(kind == ".py" for kind in e.file_types) for e in escapes):
        new = round(config.cap_fraction * config.cap_step, 2)
        out.append(
            f"Lower the cap fraction from {config.cap_fraction:.2f} to {new:.2f} (--cap-fraction in {TRIGGER_FILE}): "
            "a Python-only change escaped a narrowed selection."
        )
    return out


def _pull_log(client: CIClient, run: Run, config: CIHealthConfig) -> str:
    try:
        job = _selection_job(client, run, config)
        return client.job_log(run.repo, int(job.get("id") or 0)) if job else ""
    except CIClientError:
        return ""


def main_failure_tests(client: CIClient, run: Run, config: CIHealthConfig, store: dict) -> list[str]:
    """Failing test ids from a red main run's log, cached by run id."""
    key = str(run.id)
    if key in store:
        return list(store[key])
    ids: list[str] = []
    try:
        jobs = [j for j in client.jobs(run.repo, run.id) if str(j.get("conclusion") or "failure") == "failure"]
        patterns = tuple(p.lower() for p in config.selection_jobs)
        jobs.sort(key=lambda j: not any(p in str(j.get("name") or "").lower() for p in patterns))
        for job in jobs:
            for test_id in failing_tests(client.job_log(run.repo, int(job.get("id") or 0)) or ""):
                if test_id not in ids:
                    ids.append(test_id)
    except CIClientError:
        return []
    store[key] = ids
    return ids


def last_green_before(mains: list[Run], run: Run) -> Run | None:
    earlier = [r for r in mains if r.created_at < run.created_at and r.conclusion == "success"]
    return earlier[-1] if earlier else None


def escape_report(
    client: CIClient,
    config: CIHealthConfig,
    *,
    repo: str,
    runs: list[Run],
    pulls: list[Pull],
    cache: dict,
    failures: dict,
) -> EscapeReport:
    report = EscapeReport(window_days=config.escape_window_days, rate_warn=config.escape_rate_warn)
    workflows = selection_workflow_names(runs, config)
    mains = main_runs(runs, config, workflows)
    reads = _Reads(config.escape_log_reads)
    narrowed: dict[int, Run] = {}
    report.merges = len(pulls)
    for pull in sorted(pulls, key=lambda p: p.merged_at, reverse=True):
        run = last_pr_run(pull, runs, workflows)
        if run is None:
            continue
        marker = narrowed_marker(client, run, config, cache, reads)
        if marker is False:
            report.unread_merges += 1
        elif is_narrowed(marker) and run.conclusion == "success":
            narrowed[pull.number] = run
    report.narrowed_merges = len(narrowed)
    found: dict[int, Escape] = {}
    for red in (r for r in mains if r.conclusion == "failure"):
        green = last_green_before(mains, red)
        if green is None:
            continue
        since = [p for p in pulls if green.created_at < p.merged_at <= red.created_at]
        own = [p for p in since if p.merge_sha and p.merge_sha == red.head_sha]
        tests = main_failure_tests(client, red, config, failures)
        for pull in own or since:
            run = narrowed.get(pull.number)
            if run is None:
                continue
            ran = selected_tests(_pull_log(client, run, config))
            missed = [t for t in tests if t not in ran] if ran else list(tests)
            if tests and ran and not missed:
                continue  # the narrowed run ran these tests and passed: not an escape
            try:
                files = client.pull_files(repo, pull.number)
            except CIClientError:
                files = []
            kinds = sorted({file_type(f) for f in files})
            hit = [f for f in files if any(t.split("::", 1)[0] == f for t in missed)]
            why = (
                f"changed test file(s) {', '.join(hit)} but the narrowed run did not run the failing tests"
                if hit
                else "the failing tests were outside the narrowed selection"
            )
            prior = found.get(pull.number)
            merged = list(dict.fromkeys([*(prior.tests if prior else []), *missed]))
            found[pull.number] = Escape(pull, red, merged, kinds, why)
    report.escapes = sorted(found.values(), key=lambda e: e.pull.merged_at)
    if report.over:
        report.recommendations = recommend(report.escapes, config)
    return report


# -- red-main culprit ---------------------------------------------------------


@dataclass
class Culprit:
    run: Run
    green: Run
    pull: Pull | None
    candidates: list[Pull]
    pr_number: int
    tests: list[str]
    open: bool
    revert_url: str = ""
    revert_note: str = ""

    @property
    def since(self) -> str:
        return self.run.created_at.strftime("%Y-%m-%d %H:%MZ")

    def to_dict(self) -> dict:
        return {
            "sha": self.run.head_sha,
            "run": self.run.id,
            "run_url": self.run.url,
            "red_since": self.since,
            "open": self.open,
            "pr": self.pull.to_dict() if self.pull else ({"number": self.pr_number} if self.pr_number else None),
            "candidates": [c.number for c in self.candidates],
            "tests": self.tests,
            "revert": self.revert_url or self.revert_note,
        }


def find_culprits(
    client: CIClient, config: CIHealthConfig, *, runs: list[Run], pulls: list[Pull], failures: dict
) -> list[Culprit]:
    """Every green-to-red turn of main in the window; the last is open while main is still red."""
    workflows = selection_workflow_names(runs, config)
    mains = main_runs(runs, config, workflows)
    out: list[Culprit] = []
    for index, run in enumerate(mains):
        if run.conclusion != "failure" or index == 0 or mains[index - 1].conclusion != "success":
            continue
        green = mains[index - 1]
        since = [p for p in pulls if green.created_at < p.merged_at <= run.created_at]
        pull = next((p for p in pulls if p.merge_sha and p.merge_sha == run.head_sha), None)
        ref = _PR_REF.search(run.message)
        number = pull.number if pull else (int(ref.group(1)) if ref else 0)
        if pull is None and number:
            pull = next((p for p in pulls if p.number == number), None)
        still_red = all(r.conclusion == "failure" for r in mains[index:])
        out.append(
            Culprit(
                run=run,
                green=green,
                pull=pull,
                candidates=[] if pull else since,
                pr_number=number,
                tests=main_failure_tests(client, run, config, failures),
                open=still_red,
            )
        )
    return out


def revert_title(culprit: Culprit) -> str:
    title = culprit.pull.title if culprit.pull else f"#{culprit.pr_number}"
    return f"revert: {title} (main red since {culprit.since})"


def draft_revert(
    client: CIClient, config: CIHealthConfig, culprit: Culprit, *, already: dict, act: bool
) -> None:
    """Open a draft revert for the owner's own culprit pull request. Never merges."""
    pull = culprit.pull
    if not culprit.open or pull is None:
        return
    owners = {a.lower() for a in config.revert_authors}
    if pull.author.lower() not in owners:
        culprit.revert_note = "not drafted: not an owner pull request"
        return
    if str(pull.number) in already:
        culprit.revert_url = str(already[str(pull.number)])
        return
    if pull.title.lower().startswith("revert:"):
        culprit.revert_note = "not drafted: already a revert"
        return
    if not act or not pull.node_id:
        culprit.revert_note = "proposed: draft revert"
        return
    body = (
        f"Main has been red since {culprit.since} (first red commit {culprit.run.head_sha[:10]}, {culprit.run.url}).\n\n"
        f"Failing tests: {', '.join(culprit.tests[:10]) or 'not found in the log'}.\n\n"
        "Opened as a draft by RIVET. RIVET never merges it; review it and close it if the fix is forward."
    )
    try:
        culprit.revert_url = client.draft_revert(pull.node_id, revert_title(culprit), body)
    except CIClientError as exc:
        culprit.revert_note = f"draft revert failed: {exc}"
        return
    already[str(pull.number)] = culprit.revert_url


def load_pulls(client: CIClient, config: CIHealthConfig, repo: str, now: datetime) -> list[Pull]:
    since = (now - timedelta(days=config.escape_window_days)).date().isoformat()
    rows = client.merged_pulls(repo, since=since, base=config.default_branch)
    return [parse_pull(r) for r in rows if isinstance(r, dict) and r.get("merged_at")]
