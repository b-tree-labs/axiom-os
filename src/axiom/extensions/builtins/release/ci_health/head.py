# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The CI verdict for one head, never "the last completed run".

The default branch's concurrency group keeps one running and one pending run.
A burst of merges replaces the pending run on every merge, so the heads in
between never get a run of their own: the replaced run shows as cancelled and
never started. "The last completed run" then describes an older head, and a
reader who takes it for the branch's state is wrong both ways: a stale red
blocks pushes nobody should hold, and a stale green hides a head nobody tested
(#1782).

``head_verdict`` answers for a named head, from that head's own runs, with one
of four verdicts:

``success``     the head's own run completed and passed (on any attempt)
``failure``     the head's own run completed and failed; the failing checks
                are named
``pending``     the head has a run queued or in progress, or is the branch
                head and no run has started yet
``superseded``  no run of its own reached a verdict because a newer push
                replaced it; the newest completed head is named with its
                verdict, and whether it contains this head

A forge that cannot be read is ``unknown`` and claims nothing. Only
``failure`` is red and only ``success`` is green. ``ci_health``, the lander
and the pre-push hook (through ``release.ci_head``) all read this one answer.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from axiom.extensions.builtins.release.ci_health.client import CIClient, CIClientError
from axiom.extensions.builtins.release.ci_health.config import CIHealthConfig
from axiom.extensions.builtins.release.ci_health.detect import RED
from axiom.extensions.builtins.release.ci_health.runs import Run, parse_runs

SUCCESS = "success"
FAILURE = "failure"
PENDING = "pending"
SUPERSEDED = "superseded"
UNKNOWN = "unknown"

#: Job conclusions that make a run's failure. A cancelled job is a consequence
#: of another job's failure, not a failure of its own.
_FAILED_JOB = frozenset({"failure", "timed_out", "startup_failure", "action_required"})


@dataclass
class HeadVerdict:
    repo: str
    branch: str
    sha: str
    verdict: str
    workflow: str
    checked_at: str = ""
    run_id: int = 0
    run_url: str = ""
    run_attempt: int = 1
    failing_checks: list[str] = field(default_factory=list)
    newest_completed_sha: str = ""
    newest_completed_verdict: str = ""
    newest_completed_url: str = ""
    #: The newest completed head contains this head (the forge's compare said so).
    #: None when the forge was not asked.
    newest_contains_head: bool | None = None
    readable: bool = True
    reasons: list[str] = field(default_factory=list)

    @property
    def green(self) -> bool:
        return self.verdict == SUCCESS

    @property
    def red(self) -> bool:
        return self.verdict == FAILURE

    @property
    def after_rerun(self) -> bool:
        return self.run_attempt > 1

    def line(self) -> str:
        """One line a person or a hook can print."""
        where = f"{self.branch}@{self.sha[:10] or '?'}"
        if self.verdict == SUCCESS:
            again = " after a re-run" if self.after_rerun else ""
            return f"{where}: green{again} ({self.run_url})"
        if self.verdict == FAILURE:
            checks = ", ".join(self.failing_checks) or "checks not named in the run"
            return f"{where}: RED; failing: {checks} ({self.run_url})"
        if self.verdict == PENDING:
            if self.run_id:
                return f"{where}: pending; run {self.run_id} has not finished ({self.run_url})"
            return f"{where}: pending; no run has started for this head yet"
        if self.verdict == SUPERSEDED:
            newest = (
                f"newest completed head {self.newest_completed_sha[:10]} is "
                f"{'green' if self.newest_completed_verdict == SUCCESS else 'RED'}"
                f"{' and contains it' if self.newest_contains_head else ''}"
                f" ({self.newest_completed_url})"
                if self.newest_completed_sha
                else "no head on this branch has a completed run"
            )
            return f"{where}: untested; its run was replaced by a newer push. The {newest}"
        return f"{where}: unknown; {'; '.join(self.reasons) or 'the forge could not be read'}"

    def to_dict(self) -> dict:
        return {**asdict(self), "line": self.line()}


def _with_verdict(runs: list[Run]) -> list[Run]:
    return [r for r in runs if r.status == "completed" and (r.conclusion == SUCCESS or r.conclusion in RED)]


def _failing_jobs(client: CIClient, run: Run) -> list[str]:
    try:
        jobs = client.jobs(run.repo, run.id)
    except CIClientError:
        return []
    return [
        str(job.get("name") or "")
        for job in jobs
        if isinstance(job, dict) and str(job.get("conclusion") or "").lower() in _FAILED_JOB and job.get("name")
    ]


def head_verdict(
    client: CIClient,
    config: CIHealthConfig,
    repo: str,
    *,
    sha: str = "",
    branch: str = "",
    now: datetime | None = None,
) -> HeadVerdict:
    """The verdict for ``sha`` on ``branch`` (default: the branch's current head)."""
    now = now or datetime.now(UTC)
    branch = branch or config.default_branch
    verdict = HeadVerdict(
        repo=repo, branch=branch, sha=sha, verdict=UNKNOWN, workflow=config.main_workflow, checked_at=now.isoformat()
    )
    try:
        rows = client.workflow_runs(repo, config.main_workflow, branch, per_page=config.main_runs_read)
        branch_head = client.branch_head(repo, branch)
    except CIClientError as exc:
        verdict.readable = False
        verdict.reasons = [f"{repo}@{branch}: the forge could not be read: {exc}"]
        return verdict
    head = sha or branch_head
    verdict.sha = head
    if not head:
        verdict.readable = False
        verdict.reasons = [f"{repo}@{branch}: no head found"]
        return verdict
    runs = parse_runs(rows, repo, now=now)
    completed = _with_verdict(runs)
    newest = max(completed, key=lambda r: r.created_at) if completed else None
    if newest is not None:
        verdict.newest_completed_sha = newest.head_sha
        verdict.newest_completed_verdict = SUCCESS if newest.conclusion == SUCCESS else FAILURE
        verdict.newest_completed_url = newest.url
    own = sorted((r for r in runs if r.head_sha == head), key=lambda r: (r.created_at, r.run_attempt), reverse=True)
    for run in own:
        if run.active:
            verdict.verdict = PENDING
            verdict.run_id, verdict.run_url, verdict.run_attempt = run.id, run.url, run.run_attempt
            return verdict
        if run.status == "completed" and run.conclusion == SUCCESS:
            verdict.verdict = SUCCESS
            verdict.run_id, verdict.run_url, verdict.run_attempt = run.id, run.url, run.run_attempt
            return verdict
        if run.status == "completed" and run.conclusion in RED:
            verdict.verdict = FAILURE
            verdict.run_id, verdict.run_url, verdict.run_attempt = run.id, run.url, run.run_attempt
            verdict.failing_checks = _failing_jobs(client, run)
            return verdict
        # cancelled, skipped, neutral: no verdict of its own; keep looking.
    # No run of this head reached a verdict.
    if head == branch_head and not own:
        verdict.verdict = PENDING
        verdict.reasons = ["no run has started for this head yet"]
        return verdict
    verdict.verdict = SUPERSEDED
    verdict.reasons = [
        "its pending run was replaced by a newer push" if own else "no run was recorded for this head"
    ]
    if newest is not None and own:
        try:
            verdict.newest_contains_head = bool(client.is_ancestor(repo, head, newest.head_sha))
        except CIClientError:
            verdict.newest_contains_head = None
    return verdict
