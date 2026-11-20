# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The watch from outside the watcher.

A scheduled job on the forge reads the default branch and keeps one incident.
It shares nothing with the local heartbeat, so when the local watch dies this
one still runs.

What it can see: the default branch's newest CI verdict, the start of a red
spell and its pull request, and a branch head that moved with no fresh verdict.

What it cannot see: the local heartbeat (a file on one machine), the local
sweep, whether HERALD delivered anything, or the forge's own scheduler stopping.
"Heartbeat stale" therefore means nothing here, and the incident says so rather
than implying the local watch is healthy. The reverse direction is covered by
``outside_watch_workflow``: a local sweep alerts when this job's scheduled runs
stop.

Incidents use the marker the CI workflow already files under, so there is one
incident and not two systems. The job updates the newest open incident, which
also keeps it inside the workflow's window and stops the next failure opening
another. The status section is spliced in; whatever else the body holds stays.

Green closes only an incident this job adopted. An incident it never touched is
a person's backlog, and clearing that is a decision rather than a side effect.

Every forge write goes through ``_write``, so a later rate limit has one seam.
The category field is where failure classification lands. ``reconcile`` writes
an issue, which is the ``issue.update`` / ``issue.create`` kind of action in
ADR-045, on this job's own incident only. It merges, re-runs and updates
nothing; those are RACI actions and stay with the lander.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from axiom.extensions.builtins.release.ci_health.client import CIClient, CIClientError
from axiom.extensions.builtins.release.ci_health.config import CIHealthConfig
from axiom.extensions.builtins.release.ci_health.detect import RED as FAILED_CONCLUSIONS
from axiom.extensions.builtins.release.ci_health.runs import Run, parse_runs
from axiom.extensions.builtins.release.watch_integrity import BLIND, GREEN, RED

SECTION_BEGIN = "<!-- rivet-watch:begin -->"
SECTION_END = "<!-- rivet-watch:end -->"
UNCLASSIFIED = "unclassified"

_PR_REF = re.compile(r"\(#(\d+)\)")
_SECTION = re.compile(re.escape(SECTION_BEGIN) + r".*?" + re.escape(SECTION_END), re.DOTALL)


def marker(branch: str) -> str:
    """The marker ``ci.yml`` files its incidents under."""
    return f"<!-- ci-incident:branch={branch} -->"


@dataclass
class MainReading:
    repo: str
    branch: str
    state: str
    checked_at: str
    #: False when the forge could not be read. Nothing is concluded from that.
    readable: bool = True
    reasons: list[str] = field(default_factory=list)
    head_sha: str = ""
    verdict_sha: str = ""
    verdict_url: str = ""
    first_red_sha: str = ""
    first_red_url: str = ""
    red_since: str = ""
    #: Every run read was red, so the spell started before the read did.
    spell_open_ended: bool = False
    culprit_pr: int = 0
    #: code / main already red / stale branch / runner lost, once classified.
    category: str = UNCLASSIFIED

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _verdicts(runs: list[Run]) -> list[Run]:
    """Finished runs with a verdict, newest first. A cancelled run has none."""
    rows = [r for r in runs if r.status == "completed" and (r.conclusion == "success" or r.conclusion in FAILED_CONCLUSIONS)]
    return sorted(rows, key=lambda r: r.created_at, reverse=True)


def read_main(client: CIClient, config: CIHealthConfig, repo: str, *, now: datetime) -> MainReading:
    branch = config.default_branch
    reading = MainReading(repo=repo, branch=branch, state=BLIND, checked_at=now.isoformat())
    try:
        rows = client.workflow_runs(repo, config.main_workflow, branch, per_page=config.main_runs_read)
        reading.head_sha = client.branch_head(repo, branch)
    except CIClientError as exc:
        reading.readable = False
        reading.reasons = [f"{repo}: the forge could not be read: {exc}"]
        return reading
    verdicts = _verdicts(parse_runs(rows, repo, now=now))
    if not verdicts:
        reading.reasons = [f"{repo}@{branch}: no {config.main_workflow} verdict in the last {len(rows)} runs"]
        return reading
    newest = verdicts[0]
    reading.verdict_sha, reading.verdict_url = newest.head_sha, newest.url
    age = now - newest.created_at
    stale = bool(reading.head_sha) and reading.head_sha != newest.head_sha and age > timedelta(hours=config.main_stale_hours)
    if newest.conclusion == "success":
        if stale:
            reading.reasons = [
                f"{repo}@{branch} moved to {reading.head_sha[:10]}, and the newest verdict is "
                f"{age.total_seconds() / 3600:.0f} hours old on {newest.head_sha[:10]}"
            ]
            return reading
        reading.state = GREEN
        return reading
    reading.state = RED
    spell = [newest]
    for run in verdicts[1:]:
        if run.conclusion == "success":
            break
        spell.append(run)
    first = spell[-1]
    reading.first_red_sha, reading.first_red_url = first.head_sha, first.url
    reading.red_since = first.created_at.strftime("%Y-%m-%d %H:%MZ")
    reading.spell_open_ended = len(spell) == len(verdicts)
    found = _PR_REF.search(first.message)
    reading.culprit_pr = int(found.group(1)) if found else 0
    return reading


def render_status(reading: MainReading) -> str:
    lines = [f"### RIVET watch: {reading.state}", "", f"Checked {reading.checked_at[:16]}Z on `{reading.branch}`."]
    if reading.state == RED:
        since = f"at least {reading.red_since}" if reading.spell_open_ended else reading.red_since
        lines.append(f"Red since {since}, first red commit `{reading.first_red_sha[:10]}` ({reading.first_red_url}).")
        lines.append(f"Likely culprit: #{reading.culprit_pr}." if reading.culprit_pr else "Likely culprit: not found.")
        lines.append(f"Newest verdict: `{reading.verdict_sha[:10]}` ({reading.verdict_url}).")
        lines.append(f"Category: {reading.category}.")
    lines.extend(f"- {reason}" for reason in reading.reasons)
    lines.extend(
        [
            "",
            "This job cannot see the local heartbeat, the local sweep or HERALD delivery. "
            "It says nothing about whether those are alive.",
        ]
    )
    return "\n".join(lines)


def splice(body: str, section: str) -> str:
    """Put the status section in ``body``, replacing an earlier one."""
    block = f"{SECTION_BEGIN}\n{section}\n{SECTION_END}"
    if SECTION_BEGIN in body:
        return _SECTION.sub(lambda _m: block, body, count=1)
    return f"{body.rstrip()}\n\n{block}\n"


@dataclass
class WatchOutcome:
    reading: MainReading
    action: str = "none"
    number: int = 0
    url: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.reading.readable and not self.error

    def to_dict(self) -> dict:
        return {
            "state": self.reading.state,
            "action": self.action,
            "number": self.number,
            "url": self.url,
            "error": self.error,
            "reading": self.reading.to_dict(),
        }


def _write(fn: Callable[..., object], *args: object, **kwargs: object) -> object:
    """The one door for a forge write from this job."""
    return fn(*args, **kwargs)


def open_incidents(client: CIClient, config: CIHealthConfig, repo: str) -> list[dict]:
    """Open default-branch incidents, newest first."""
    tag = marker(config.default_branch)
    rows = [i for i in client.open_issues(repo, config.incident_labels[0]) if tag in str(i.get("body") or "")]
    return sorted(rows, key=lambda i: int(i.get("number") or 0), reverse=True)


def reconcile(client: CIClient, config: CIHealthConfig, reading: MainReading, *, act: bool) -> WatchOutcome:
    """Bring the one incident in line with the reading."""
    outcome = WatchOutcome(reading)
    repo = reading.repo
    incidents = open_incidents(client, config, repo)
    current = incidents[0] if incidents else None
    if reading.state == GREEN:
        adopted = next((i for i in incidents if SECTION_BEGIN in str(i.get("body") or "")), None)
        if adopted is None:
            return outcome
        outcome.number = int(adopted["number"])
        if not act:
            outcome.action = "would_close"
            return outcome
        _write(
            client.comment_issue,
            repo,
            outcome.number,
            f"`{reading.branch}` is green at `{reading.verdict_sha[:10]}`. Closing this incident.",
        )
        _write(client.close_issue, repo, outcome.number)
        outcome.action = "closed"
        return outcome
    section = render_status(reading)
    older = len(incidents) - 1
    if older > 0:
        section += f"\n\n{older} older incident(s) on `{reading.branch}` are still open."
    if current is None:
        if not act:
            outcome.action = "would_open"
            return outcome
        short = (reading.first_red_sha or reading.head_sha)[:7]
        title = (
            f"🔴 CI incident on `{reading.branch}` (first: {short})"
            if reading.state == RED
            else f"⚠️ RIVET watch is blind on `{reading.branch}`"
        )
        made = _write(
            client.create_issue,
            repo,
            title,
            splice(marker(reading.branch), section),
            labels=config.incident_labels,
            assignees=config.incident_assignees,
        )
        outcome.action = "opened"
        outcome.number = int(made.get("number") or 0)
        outcome.url = str(made.get("html_url") or "")
        return outcome
    outcome.number = int(current["number"])
    outcome.url = str(current.get("html_url") or "")
    if not act:
        outcome.action = "would_update"
        return outcome
    _write(client.edit_issue, repo, outcome.number, body=splice(str(current.get("body") or ""), section))
    outcome.action = "updated"
    return outcome


def watch_main(client: CIClient, config: CIHealthConfig, repo: str, *, now: datetime, act: bool) -> WatchOutcome:
    """One tick. A forge that cannot be read is an error, never an incident."""
    reading = read_main(client, config, repo, now=now)
    if not reading.readable:
        return WatchOutcome(reading, error=reading.reasons[0])
    try:
        return reconcile(client, config, reading, act=act)
    except CIClientError as exc:
        return WatchOutcome(reading, error=f"incident: {exc}")
