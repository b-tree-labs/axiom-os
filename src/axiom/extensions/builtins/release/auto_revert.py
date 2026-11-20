# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Main heals itself: a commit that breaks main is reverted (LOOP-S4, ADR-257 D6).

A main commit whose required workflow fails (``ci_health.auto_revert_workflows``:
CI and the server-side guards here) is judged by ``decide`` and handled by
``carry_out``. Nothing is reverted until the failure is shown to be real and to be
this commit's:

1. **Head-run truth** (``ci_health.head``): the commit's own run failed. A cancelled,
   superseded or still-running run is not a failure; a failed run that names no
   failing check is asked about, never reverted.
2. **One re-run of a known flake** (``ci_health.flake``): a test's first failure on
   main, in a file the commit did not touch, is re-run once. When the re-run finishes
   red, the workflow completes again and this judges it again as real.
3. **Bisect over the commits since the last green head**, by the first-parent chain:
   when an earlier commit in that range has its own red run, main's failure predates
   this commit and nothing is done; when a commit in between has no verdict of its
   own, the range is ambiguous and a person is asked.

The limits: a commit that is itself a revert is never reverted (a revert of a revert
needs a person); at most one commit is reverted per failure, and a commit already
reverted on main is left alone; nothing is reverted while the release lock is held.

The revert is an ordinary commit on main that names the reverted commit, the failing
check and run, and the author, with a ``Bypass-Reason`` trailer (it is the push that
fixes red main). Then an ask goes to the author: one issue assigned to them, found
again by its marker rather than duplicated, and a notification to them through HERALD,
which puts it in their Needs you (ADR-253). A revert that does not apply or push
becomes an ask instead.

``carry_out`` writes nothing without ``act``. The skill also requires the repository
variable ``AXIOM_AUTO_REVERT=1`` before it passes ``act``, so the owner turns it on
deliberately. The git side never force-pushes.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from axiom.extensions.builtins.release.ci_health import escape, flake
from axiom.extensions.builtins.release.ci_health.alerts import Alert, send_alerts
from axiom.extensions.builtins.release.ci_health.client import CIClient, CIClientError
from axiom.extensions.builtins.release.ci_health.config import CIHealthConfig
from axiom.extensions.builtins.release.ci_health.head import FAILURE, PENDING, SUCCESS, head_verdict
from axiom.extensions.builtins.release.ci_health.runs import parse_runs

NONE = "none"
WAIT = "wait"
RERUN = "rerun"
REVERT = "revert"
ASK = "ask"
HELD = "held"

_REVERT_SUBJECT = re.compile(r'^(revert\b[:( ]|revert "|reapply ")', re.IGNORECASE)


class RevertFailed(RuntimeError):
    """The revert did not apply or did not push. A person decides."""


class RevertGit(Protocol):
    def revert_and_push(self, sha: str, *, mainline: int | None, message: str, branch: str) -> str: ...


def is_revert(message: str) -> bool:
    """True for a commit that reverts another: its subject or its git revert line says so."""
    subject = (message or "").strip().splitlines()[0] if (message or "").strip() else ""
    return bool(_REVERT_SUBJECT.match(subject)) or "This reverts commit " in (message or "")


@dataclass
class RevertDecision:
    repo: str
    sha: str
    action: str = NONE
    reasons: list[str] = field(default_factory=list)
    verdicts: list[str] = field(default_factory=list)
    workflow: str = ""
    run_id: int = 0
    run_url: str = ""
    failing_checks: list[str] = field(default_factory=list)
    author: str = ""
    subject: str = ""
    parents: list[str] = field(default_factory=list)
    last_green: str = ""
    untested: list[str] = field(default_factory=list)
    flake: dict = field(default_factory=dict)
    lock: str = ""
    acted: bool = False
    dry_run: str = ""
    revert_sha: str = ""
    ask_url: str = ""
    writes: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    _reading: Any = field(default=None, repr=False, compare=False)

    def render(self) -> str:
        lines = [f"auto-revert: {self.sha[:10]} on {self.repo}: {self.action}"]
        lines += [f"  - {reason}" for reason in self.reasons]
        if self.run_url:
            checks = ", ".join(self.failing_checks) or "no check named"
            lines.append(f"  run: {self.run_url} ({self.workflow}; failing: {checks})")
        if self.author:
            lines.append(f"  author: @{self.author}")
        if self.revert_sha:
            lines.append(f"  reverted by {self.revert_sha[:10]}")
        if self.ask_url:
            lines.append(f"  ask: {self.ask_url}")
        lines += [f"  error: {error}" for error in self.errors]
        if not self.acted:
            lines.append(f"  ({self.dry_run or 'dry run'}: nothing was written)")
        return "\n".join(lines) + "\n"

    def to_dict(self) -> dict:
        data = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        data["line"] = self.render().splitlines()[0]
        return data


def _first_line(message: str) -> str:
    text = (message or "").strip()
    return text.splitlines()[0] if text else ""


def _first_parent_chain(client: CIClient, repo: str, green: str, sha: str, limit: int) -> list[str] | None:
    """Commits after ``green`` up to ``sha`` on the first-parent line, oldest first. None when not reached."""
    chain: list[str] = []
    current = sha
    for _ in range(limit):
        chain.insert(0, current)
        parents = client.commit(repo, current).get("parents") or []
        if not parents:
            return None
        if parents[0] == green:
            return chain
        current = parents[0]
    return None


def decide(
    client: CIClient,
    config: CIHealthConfig,
    repo: str,
    sha: str,
    *,
    lock: Any = None,
    now: datetime | None = None,
) -> RevertDecision:
    """What to do about ``sha`` on the default branch. Reads only."""
    now = now or datetime.now(UTC)
    branch = config.default_branch
    decision = RevertDecision(repo=repo, sha=sha)
    try:
        return _decide(client, config, decision, branch=branch, lock=lock, now=now)
    except CIClientError as exc:
        decision.action = NONE
        decision.errors.append(f"the forge could not be read: {exc}")
        decision.reasons.append("nothing is concluded from a forge that cannot be read")
        return decision


def _decide(client, config, decision: RevertDecision, *, branch: str, lock: Any, now: datetime) -> RevertDecision:
    repo, sha = decision.repo, decision.sha
    if len(sha) < 40:
        # Runs carry full shas; a short one would read as "no run of its own".
        sha = str(client.commit(repo, sha).get("sha") or sha)
        decision.sha = sha
    red = None
    pending = False
    for workflow in config.auto_revert_workflows:
        wcfg = replace(config, main_workflow=workflow)
        verdict = head_verdict(client, wcfg, repo, sha=sha, branch=branch, now=now)
        decision.verdicts.append(f"{workflow}: {verdict.line()}")
        if not verdict.readable and any("HTTP 404" in reason for reason in verdict.reasons):
            # A required workflow this repository does not have (yet) has no verdict to
            # give; the others still decide. Any other read failure concludes nothing.
            decision.reasons.append(f"{workflow} is not on this repository; it was skipped")
            continue
        if not verdict.readable:
            decision.errors.extend(verdict.reasons)
            decision.reasons.append("nothing is concluded from a forge that cannot be read")
            return decision
        if verdict.verdict == FAILURE and red is None:
            red = (workflow, wcfg, verdict)
        elif verdict.verdict == PENDING:
            pending = True
    if red is None:
        if pending:
            decision.action = WAIT
            decision.reasons.append("a required run on this commit has not finished")
        else:
            decision.reasons.append(
                "not a failure of this commit's own run (green, cancelled or replaced by a newer push): "
                + "; ".join(decision.verdicts)
            )
        return decision

    workflow, wcfg, verdict = red
    decision.workflow, decision.run_id, decision.run_url = workflow, verdict.run_id, verdict.run_url
    decision.failing_checks = list(verdict.failing_checks)
    info = client.commit(repo, sha)
    decision.author = str(info.get("author_login") or "")
    decision.subject = _first_line(str(info.get("message") or ""))
    decision.parents = list(info.get("parents") or [])

    # At most one revert per failure: a commit already reverted on main is left alone.
    head = client.branch_head(repo, branch)
    if head and head != sha:
        for later in client.commits_between(repo, sha, head):
            if f"This reverts commit {sha}" in str(later.get("message") or ""):
                decision.reasons.append(f"already reverted on {branch} by {str(later.get('sha'))[:10]}")
                return decision

    if not decision.failing_checks:
        decision.action = ASK
        decision.reasons.append(
            "the run failed but named no failing check; an empty or broken run is not proof the commit broke main"
        )
        return decision

    # A known flake is re-run once before it counts.
    rows = client.workflow_runs(repo, workflow, branch, per_page=config.main_runs_read)
    runs = parse_runs(rows, repo, now=now)
    run = next((r for r in runs if r.id == verdict.run_id), None)
    if run is not None:
        store: dict = {}
        tests = escape.main_failure_tests(client, run, wcfg, store)
        record = {"attempt_before": run.run_attempt - 1, "tests": tests, "issues": {}} if run.run_attempt > 1 else None
        reading = flake.classify(
            client, wcfg, run=run, tests=tests, mains=flake.main_runs(runs, wcfg), failures=store, record=record
        )
        decision._reading = reading
        decision.flake = {"verdict": reading.verdict, "tests": list(reading.tests), "reasons": list(reading.reasons)}
        if reading.verdict == flake.CANDIDATE:
            if config.flake_rerun:
                decision.action = RERUN
                decision.reasons.append("looks like a known flake; its failed jobs are re-run once before it counts")
            else:
                decision.action = ASK
                decision.reasons.append("looks like a flake and ci_health.flake_rerun is off; a person decides")
            return decision
        if reading.verdict == flake.RERUN_REQUESTED:
            decision.action = WAIT
            decision.reasons.append("the flake re-run has not finished")
            return decision
        if reading.verdict == flake.FLAKE:
            decision.reasons.append("passed when its failed jobs were re-run: a flake, not a breaking commit")
            return decision
        if reading.verdict == flake.UNCLASSIFIED and reading.tests:
            decision.action = ASK
            decision.reasons.append("whether this is a flake could not be told: " + "; ".join(reading.reasons))
            return decision

    if is_revert(str(info.get("message") or "")):
        decision.action = ASK
        decision.reasons.append("this commit is itself a revert; a revert of a revert needs a person")
        return decision

    # Bisect over the commits since the last green head of this workflow.
    greens = sorted(
        (r for r in runs if r.status == "completed" and r.conclusion == SUCCESS and r.head_sha != sha),
        key=lambda r: r.created_at,
        reverse=True,
    )
    green = next((r.head_sha for r in greens if client.is_ancestor(repo, r.head_sha, sha)), "")
    if not green:
        decision.action = ASK
        decision.reasons.append(
            f"no green {workflow} head before this commit in the last {len(runs)} runs; where main broke is not known"
        )
        return decision
    decision.last_green = green
    chain = _first_parent_chain(client, repo, green, sha, config.auto_revert_max_chain)
    if chain is None:
        decision.action = ASK
        decision.reasons.append(
            f"the last green head {green[:10]} is not within {config.auto_revert_max_chain} first-parent commits; "
            "where main broke is ambiguous"
        )
        return decision
    for earlier in chain[:-1]:
        before = head_verdict(client, wcfg, repo, sha=earlier, branch=branch, now=now)
        if before.verdict == FAILURE:
            decision.reasons.append(
                f"main's failure predates this commit: {earlier[:10]} was already red ({before.run_url}); "
                "that commit's own run decides"
            )
            return decision
        if before.verdict == SUCCESS:
            decision.untested = []
            continue
        decision.untested.append(earlier)
    if decision.untested:
        decision.action = ASK
        decision.reasons.append(
            "ambiguous: commits between the last green head and this one have no verdict of their own: "
            + ", ".join(s[:10] for s in decision.untested)
        )
        return decision

    if lock is not None:
        describe = getattr(lock, "describe", None)
        decision.lock = describe() if callable(describe) else str(lock)
        decision.action = HELD
        decision.reasons.append(f"would revert, but nothing is reverted while the release lock is held: {decision.lock}")
        return decision
    decision.action = REVERT
    decision.reasons.append(
        f"{sha[:10]} is the first red commit after the last green head {green[:10]}, and the failure is real"
    )
    return decision


def revert_message(decision: RevertDecision, config: CIHealthConfig) -> str:
    checks = ", ".join(decision.failing_checks) or "a required check"
    flake_note = (
        "its failure repeated when the failed jobs were re-run"
        if decision.flake.get("verdict") == flake.REAL and decision.flake.get("tests")
        else "the failure was confirmed as real"
    )
    author = f"@{decision.author}" if decision.author else "unknown (no forge login on the commit)"
    return (
        f'Revert "{decision.subject or decision.sha}"\n\n'
        f"This reverts commit {decision.sha}.\n\n"
        f"Main went red on this commit: {checks} failed in {decision.workflow} ({decision.run_url}).\n"
        f"Author: {author}\n\n"
        f"Reverted automatically (ADR-257 D6): {flake_note}, and it is the first red commit after "
        f"the last green head {decision.last_green[:10]}. A revert of this revert needs a person.\n\n"
        f"Auto-Revert-Of: {decision.sha}\n"
        f"Bypass-Reason: automatic revert of {decision.sha[:10]}, which turned {config.default_branch} red "
        f"({decision.run_url})\n"
    )


def _marker(sha: str) -> str:
    return f"<!-- loop-auto-revert:sha={sha} -->"


def _ask_body(decision: RevertDecision) -> str:
    checks = ", ".join(decision.failing_checks) or "no check named"
    what = (
        f"It was reverted automatically by `{decision.revert_sha[:10]}` (`{decision.revert_sha}`)."
        if decision.revert_sha
        else "It was **not** reverted; a person decides."
    )
    need = (
        "Fix the failure and push the change again with its fix. A revert of the revert needs a person."
        if decision.revert_sha
        else "Revert it, fix it forward, or say why main's red is not this commit's."
    )
    lines = [
        _marker(decision.sha),
        f"Main went red on `{decision.sha}`"
        + (f" ({decision.subject})" if decision.subject else "")
        + (f", by @{decision.author}" if decision.author else "")
        + ".",
        "",
        f"- Failing: {checks} in `{decision.workflow}` ({decision.run_url or 'no run'})",
        f"- {what}",
        "",
        f"**Needed from you:** {need}",
        "",
        "What was found:",
        *[f"- {reason}" for reason in decision.reasons],
    ]
    return "\n".join(lines)


def _ask(client: CIClient, config: CIHealthConfig, decision: RevertDecision, *, sender: Callable | None) -> None:
    repo = decision.repo
    label = config.auto_revert_labels[0] if config.auto_revert_labels else "auto-revert"
    marker = _marker(decision.sha)
    body = _ask_body(decision)
    try:
        existing = [i for i in client.open_issues(repo, label) if marker in str(i.get("body") or "")]
        if existing:
            number = int(existing[0].get("number") or 0)
            client.comment_issue(repo, number, body)
            decision.ask_url = str(existing[0].get("html_url") or "")
            decision.writes.append({"action": "ask_comment", "number": number})
        else:
            title = (
                f"Main went red on {decision.sha[:10]}; it was reverted automatically"
                if decision.revert_sha
                else f"Main went red on {decision.sha[:10]}; a person decides"
            )
            assignees = [decision.author] if decision.author else list(config.incident_assignees)
            try:
                made = client.create_issue(repo, title, body, labels=config.auto_revert_labels, assignees=assignees)
            except CIClientError:
                made = client.create_issue(
                    repo, title, body, labels=config.auto_revert_labels, assignees=list(config.incident_assignees)
                )
            decision.ask_url = str(made.get("html_url") or "")
            decision.writes.append({"action": "ask_open", "number": int(made.get("number") or 0)})
    except CIClientError as exc:
        decision.errors.append(f"the ask was not opened: {exc}")
    recipients = ((f"@{decision.author}:github",) if decision.author else ()) + tuple(config.notify_recipients)
    alert = Alert(
        "auto_revert",
        "high",
        f"Main went red on {decision.sha[:10]}" + (f" ({decision.subject})" if decision.subject else ""),
        f"reverted it with {decision.revert_sha[:10]}" if decision.revert_sha else "did not revert it",
        "Fix and push again." if decision.revert_sha else "Revert it, fix forward, or say why it is not this commit.",
        f"auto-revert:{decision.sha}:{decision.action}",
        detail=decision.ask_url,
    )
    # HERALD writes the inbox row that puts the ask in the author's Needs you. A failed
    # send is logged by send_alerts and never stops the revert.
    for sent in send_alerts([alert], recipients, already_sent=set(), sender=sender):
        decision.writes.append({"action": "notify", "sent": bool(sent.get("sent")), "recipients": list(recipients)})


def carry_out(
    client: CIClient,
    config: CIHealthConfig,
    decision: RevertDecision,
    *,
    git: RevertGit | None,
    act: bool,
    sender: Callable | None = None,
    dry_reason: str = "",
) -> RevertDecision:
    """Do what ``decision`` says. Without ``act`` nothing is written."""
    if not act:
        decision.dry_run = dry_reason or "dry run"
        return decision
    decision.acted = True
    if decision.action == RERUN and decision._reading is not None:
        wcfg = replace(config, main_workflow=decision.workflow)
        decision.writes.extend(
            flake.act(client, wcfg, [decision._reading], repo=decision.repo, records={}, act=True, allow_rerun=True)
        )
        return decision
    if decision.action == REVERT:
        if git is None:
            decision.action = ASK
            decision.reasons.append("no git checkout to revert in")
        else:
            try:
                decision.revert_sha = git.revert_and_push(
                    decision.sha,
                    mainline=1 if len(decision.parents) > 1 else None,
                    message=revert_message(decision, config),
                    branch=config.default_branch,
                )
                decision.writes.append({"action": "revert", "sha": decision.revert_sha})
            except RevertFailed as exc:
                decision.action = ASK
                decision.reasons.append(f"the revert did not apply or push: {exc}")
        _ask(client, config, decision, sender=sender)
        return decision
    if decision.action == ASK:
        _ask(client, config, decision, sender=sender)
    return decision


class GitRevert:
    """Revert one commit on a fresh copy of the branch and push it. Never force-pushes."""

    def __init__(self, root: Path, *, remote: str = "origin", env: dict | None = None) -> None:
        self.root = Path(root)
        self.remote = remote
        self.env = env

    def _git(self, *args: str) -> str:
        if any(a.startswith(("--force", "+")) or a == "-f" for a in args):
            raise RevertFailed("force flags are not allowed here")
        done = subprocess.run(
            ["git", *args],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        if done.returncode != 0:
            detail = (done.stderr or done.stdout or "").strip().splitlines()
            raise RevertFailed(f"git {args[0]}: {detail[-1] if detail else done.returncode}")
        return done.stdout.strip()

    def revert_and_push(self, sha: str, *, mainline: int | None, message: str, branch: str) -> str:
        self._git("fetch", "-q", self.remote, branch)
        self._git("checkout", "-q", "--detach", f"{self.remote}/{branch}")
        args = ["revert", "--no-edit", "--no-commit", *(["-m", str(mainline)] if mainline else []), sha]
        try:
            self._git(*args)
        except RevertFailed:
            try:
                self._git("revert", "--abort")
            except RevertFailed:
                pass
            raise
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as handle:
            handle.write(message)
            path = handle.name
        try:
            self._git("commit", "-q", "-F", path)
        finally:
            Path(path).unlink(missing_ok=True)
        new = self._git("rev-parse", "HEAD")
        self._git("push", "-q", self.remote, f"HEAD:refs/heads/{branch}")
        return new


__all__ = [
    "ASK",
    "HELD",
    "NONE",
    "RERUN",
    "REVERT",
    "WAIT",
    "GitRevert",
    "RevertDecision",
    "RevertFailed",
    "carry_out",
    "decide",
    "is_revert",
    "revert_message",
]
