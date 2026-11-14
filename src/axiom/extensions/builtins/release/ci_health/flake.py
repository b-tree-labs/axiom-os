# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Flake handling on the default branch (#1782).

A red main run is a **flake** when three things hold:

1. the failing test's first failure on main in the last ``flake_history_runs``
   runs of the main workflow;
2. the test's file was not changed by the merge that turned main red;
3. a re-run of only the failed jobs passes.

The first two are read. The third RIVET does itself, once: ``rerun_failed``
(``gh run rerun --failed``) on the one run, recorded so it is never asked for
twice. When the re-run passes, RIVET files or updates one issue per test titled
``flake: <test id>`` and ``ci_health`` reports the run as green after the
re-run with the flake named: the head's own run passed, so the head verdict is
``success`` and the release decision reads main as green. Anything else (failed
before, file changed, failed again, nothing to go on) is a **real** red and the
red-main rule applies unchanged.

``gather`` classifies and never writes. ``sweep`` with ``act`` requests the
re-run and files the issue. Records live in the budget state file under
``flakes``, keyed by run id.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from axiom.extensions.builtins.release.ci_health import escape
from axiom.extensions.builtins.release.ci_health.client import CIClient, CIClientError
from axiom.extensions.builtins.release.ci_health.config import CIHealthConfig
from axiom.extensions.builtins.release.ci_health.detect import RED
from axiom.extensions.builtins.release.ci_health.runs import Run

CANDIDATE = "candidate"
RERUN_REQUESTED = "rerun-requested"
FLAKE = "flake"
REAL = "real"
UNCLASSIFIED = "unclassified"

#: Re-run records kept in the state file.
MAX_RECORDS = 50


@dataclass
class FlakeReading:
    repo: str
    run_id: int
    sha: str
    run_url: str
    tests: list[str]
    verdict: str
    reasons: list[str] = field(default_factory=list)
    first_failure: bool | None = None
    file_unchanged: bool | None = None
    run_attempt: int = 1
    attempt_before: int = 0
    rerun_requested_at: str = ""
    #: test id -> issue url, once filed.
    issues: dict[str, str] = field(default_factory=dict)

    def line(self) -> str:
        tests = ", ".join(self.tests[:5]) or "no test ids"
        if self.verdict == FLAKE:
            links = ", ".join(self.issues.values())
            return f"run {self.run_id} on {self.sha[:10]}: flake, green after a re-run; {tests}" + (f" ({links})" if links else "")
        if self.verdict == CANDIDATE:
            return f"run {self.run_id} on {self.sha[:10]}: looks like a flake ({tests}); a re-run of the failed jobs decides"
        if self.verdict == RERUN_REQUESTED:
            return f"run {self.run_id} on {self.sha[:10]}: re-run of the failed jobs requested ({tests}); waiting on its verdict"
        if self.verdict == REAL:
            return f"run {self.run_id} on {self.sha[:10]}: real red ({tests}); {'; '.join(self.reasons)}"
        return f"run {self.run_id} on {self.sha[:10]}: unclassified; {'; '.join(self.reasons)}"

    def to_dict(self) -> dict:
        return {**asdict(self), "line": self.line()}


def test_file(test_id: str) -> str:
    return test_id.split("::", 1)[0]


def main_runs(runs: list[Run], config: CIHealthConfig) -> list[Run]:
    """Finished default-branch push runs of the main workflow, oldest first.

    The workflow is ``ci_health.main_workflow`` by path (the rule the head verdict
    uses), with the selection workflow as the fallback when no run matches it.
    """
    chosen = [r for r in runs if r.path.endswith(config.main_workflow)]
    if not chosen:
        names = escape.selection_workflow_names(runs, config)
        chosen = [r for r in runs if r.workflow in names]
    rows = [
        r
        for r in chosen
        if r.event == "push"
        and r.head_branch == config.default_branch
        and r.status == "completed"
        and r.conclusion in {"success", "failure"}
    ]
    return sorted(rows, key=lambda r: r.created_at)


def _earlier_failures(
    client: CIClient, config: CIHealthConfig, *, run: Run, tests: list[str], mains: list[Run], failures: dict
) -> list[str]:
    """Tests among ``tests`` that already failed in the last N main runs before ``run``."""
    earlier = [r for r in mains if r.created_at < run.created_at][-config.flake_history_runs :]
    seen: list[str] = []
    for prior in earlier:
        if prior.conclusion != "failure":
            continue
        ids = escape.main_failure_tests(client, prior, config, failures)
        seen.extend(t for t in tests if t in ids and t not in seen)
    return seen


def classify(
    client: CIClient,
    config: CIHealthConfig,
    *,
    run: Run,
    tests: list[str],
    mains: list[Run],
    failures: dict,
    record: dict | None,
) -> FlakeReading:
    """One run's reading. Reads only; the re-run is ``act``'s."""
    reading = FlakeReading(
        repo=run.repo, run_id=run.id, sha=run.head_sha, run_url=run.url, tests=list(tests), verdict=UNCLASSIFIED,
        run_attempt=run.run_attempt,
    )
    if record:
        reading.attempt_before = int(record.get("attempt_before") or 0)
        reading.rerun_requested_at = str(record.get("requested_at") or "")
        reading.issues = dict(record.get("issues") or {})
    if not tests:
        reading.reasons = ["no test ids in the failed job's log"]
        return reading
    repeats = _earlier_failures(client, config, run=run, tests=tests, mains=mains, failures=failures)
    reading.first_failure = not repeats
    if repeats:
        reading.verdict = REAL
        reading.reasons = [f"failed before on main in the last {config.flake_history_runs} runs: {', '.join(repeats)}"]
        return reading
    try:
        changed = set(client.commit_files(run.repo, run.head_sha))
    except CIClientError as exc:
        reading.reasons = [f"the merge's files could not be read: {exc}"]
        return reading
    hits = [t for t in tests if test_file(t) in changed]
    reading.file_unchanged = not hits
    if hits:
        reading.verdict = REAL
        reading.reasons = ["the merge changed the failing test's file: " + ", ".join(sorted({test_file(t) for t in hits}))]
        return reading
    if not record:
        reading.verdict = CANDIDATE
        reading.reasons = ["first failure on main and the test file is unchanged by the merge; a re-run of the failed jobs decides"]
        return reading
    if run.run_attempt > reading.attempt_before:
        if run.status == "completed" and run.conclusion == "success":
            reading.verdict = FLAKE
            reading.reasons = ["passed when only the failed jobs were re-run"]
        elif run.status == "completed" and run.conclusion in RED:
            reading.verdict = REAL
            reading.reasons = ["failed again when only the failed jobs were re-run"]
        else:
            reading.verdict = RERUN_REQUESTED
            reading.reasons = ["the re-run is still running"]
        return reading
    reading.verdict = RERUN_REQUESTED
    reading.reasons = ["the re-run was requested and has not started"]
    return reading


def assess(
    client: CIClient,
    config: CIHealthConfig,
    *,
    repo: str,
    runs: list[Run],
    failures: dict,
    records: dict,
) -> list[FlakeReading]:
    """Readings for main's newest verdict when it is red, and for every run with a re-run record.

    An older red whose spell a later green already closed is history, not a
    candidate: only a red that is main's current verdict gets the re-run.
    """
    mains = main_runs(runs, config)
    wanted: dict[int, Run] = {}
    if mains and mains[-1].conclusion == "failure":
        wanted[mains[-1].id] = mains[-1]
    by_id = {r.id: r for r in runs if r.repo == repo}
    for key in records:
        run = by_id.get(int(key)) if str(key).isdigit() else None
        if run is not None and run.repo == repo:
            wanted[run.id] = run
    out: list[FlakeReading] = []
    for run in sorted(wanted.values(), key=lambda r: r.created_at):
        record = records.get(str(run.id))
        tests = escape.main_failure_tests(client, run, config, failures) if run.conclusion == "failure" else []
        if not tests and record:
            tests = list(record.get("tests") or [])
        out.append(classify(client, config, run=run, tests=tests, mains=mains, failures=failures, record=record))
    return out


def flake_issue(client: CIClient, config: CIHealthConfig, repo: str, test_id: str, reading: FlakeReading) -> str:
    """File or update the one issue for ``test_id``. Returns its url."""
    title = f"flake: {test_id}"
    label = config.flake_labels[0] if config.flake_labels else "flake"
    existing = [i for i in client.open_issues(repo, label) if str(i.get("title") or "") == title]
    if existing:
        number = int(existing[0].get("number") or 0)
        client.comment_issue(
            repo,
            number,
            f"Flaked again on `{config.default_branch}` at `{reading.sha[:10]}` ({reading.run_url}); "
            "passed when RIVET re-ran only the failed jobs.",
        )
        return str(existing[0].get("html_url") or "")
    body = (
        f"`{test_id}` failed on `{config.default_branch}` at `{reading.sha[:10]}` ({reading.run_url}) and passed when "
        "RIVET re-ran only the failed jobs.\n\n"
        f"Why it is a flake and not a real red: its first failure on `{config.default_branch}` in the last "
        f"{config.flake_history_runs} runs, and the merge did not change its file.\n\n"
        "RIVET re-runs a flake once and does not count a passed re-run as red for the release decision. "
        "Fix or quarantine the test; close this when it no longer flakes."
    )
    made = client.create_issue(repo, title, body, labels=config.flake_labels, assignees=config.incident_assignees)
    return str(made.get("html_url") or "")


def act(
    client: CIClient,
    config: CIHealthConfig,
    readings: list[FlakeReading],
    *,
    repo: str,
    records: dict,
    act: bool,
    allow_rerun: bool,
    now: datetime | None = None,
) -> list[dict]:
    """Request the one re-run for each candidate; file the issue for each confirmed flake."""
    now = now or datetime.now(UTC)
    actions: list[dict] = []
    for reading in readings:
        if reading.repo != repo:
            continue
        key = str(reading.run_id)
        if reading.verdict == CANDIDATE:
            if not act:
                continue
            if not config.flake_rerun:
                reading.reasons.append("re-run not requested: ci_health.flake_rerun is off")
                continue
            if not allow_rerun:
                reading.reasons.append("re-run not requested: the CI budget is at stop")
                continue
            try:
                client.rerun_failed(repo, reading.run_id)
            except CIClientError as exc:
                reading.reasons.append(f"re-run not requested: {exc}")
                continue
            records[key] = {
                "sha": reading.sha,
                "tests": list(reading.tests),
                "attempt_before": reading.run_attempt,
                "requested_at": now.isoformat(),
                "issues": {},
            }
            reading.verdict = RERUN_REQUESTED
            reading.attempt_before = reading.run_attempt
            reading.rerun_requested_at = now.isoformat()
            reading.reasons.append("re-ran only the failed jobs, once")
            actions.append({"action": "rerun_failed", "run": reading.run_id, "sha": reading.sha, "tests": list(reading.tests)})
        elif reading.verdict == FLAKE and act:
            record = records.setdefault(key, {"sha": reading.sha, "tests": list(reading.tests), "attempt_before": reading.attempt_before, "requested_at": reading.rerun_requested_at, "issues": {}})
            issues = record.setdefault("issues", {})
            for test_id in reading.tests:
                if test_id in issues:
                    continue
                try:
                    issues[test_id] = flake_issue(client, config, repo, test_id, reading)
                except CIClientError as exc:
                    reading.reasons.append(f"issue not filed for {test_id}: {exc}")
                    continue
                actions.append({"action": "flake_issue", "run": reading.run_id, "test": test_id, "url": issues[test_id]})
            reading.issues = dict(issues)
    return actions


def trim(records: dict) -> dict:
    """Keep the newest records only."""
    keys = sorted((k for k in records if str(k).isdigit()), key=int)[-MAX_RECORDS:]
    return {k: records[k] for k in keys}
