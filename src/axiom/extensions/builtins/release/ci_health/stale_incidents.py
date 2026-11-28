# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One incident per cause: an auto-opened CI incident closes when its cause is gone.

The CI workflow files ``CI incident on <key>`` under the marker ``<!-- ci-incident:branch=<key> -->``,
for a failure on the default branch or on a version tag. ``main_watch`` closes only the incident it
adopted, so the rest stayed open after main turned green and after later tags published. This closes
them, by rule and with a comment that says why:

* **default branch**: a completed green run of the main workflow exists on a commit that is a strict
  descendant of the incident's newest occurrence. The newest green run is the one asked about: when it
  is not a descendant, no earlier one is, because main is one line of history.
* **Semgrep** (key ``semgrep``, filed by the Semgrep workflow for a red scan on main or the nightly): the
  newest completed push, scheduled or dispatched Semgrep run on the default branch is green and was
  created after the incident's newest occurrence. A later red run keeps it open.
* **version tag**: a later version tag has a completed successful run of the publish workflow.

Never closed: an incident whose marker and title name different keys, one with no marker, one a person
filed, one whose key is neither the default branch nor a version tag. Kept when unsure: an occurrence
commit that cannot be resolved, no later green run among the runs read, no later successful publish.
A person's comment is not hidden: the close comment names who commented. ``scan`` reads only; ``close``
is the only write, and only for a finding that says ``close``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from axiom.extensions.builtins.release.ci_health.client import CIClient, CIClientError
from axiom.extensions.builtins.release.ci_health.config import CIHealthConfig

CLOSE = "close"
KEEP = "keep"
SKIP = "skip"

_MARKER = re.compile(r"<!-- ci-incident:branch=(\S+?) -->")
_TITLE = re.compile(r"CI incident on `([^`]+)`")
_OCCURRENCE = re.compile(r"\*\*Commit\*\* \| `([0-9a-f]{7,40})`")
_FIRST = re.compile(r"\(first: ([0-9a-f]{7,40})\)")
_AT = re.compile(r"\*\*At \(UTC\)\*\* \| (\d{4}-\d\d-\d\dT[\d:]+Z)")
SEMGREP_KEY = "semgrep"
_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
#: How many later tags are asked about, newest first, before an incident is kept.
MAX_TAGS_ASKED = 12


@dataclass
class Finding:
    number: int
    title: str
    key: str = ""
    verdict: str = SKIP
    reason: str = ""
    comment: str = ""
    later_run: str = ""
    later_sha: str = ""
    persons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def _version(name: str) -> tuple[int, int, int] | None:
    found = _VERSION.match(name)
    return (int(found.group(1)), int(found.group(2)), int(found.group(3))) if found else None


def _is_bot(user: object) -> bool:
    if not isinstance(user, dict):
        return False
    return str(user.get("type") or "") == "Bot" or str(user.get("login") or "").endswith("[bot]")


def _key(issue: dict) -> tuple[str, str]:
    """The incident's key, or ``("", why)`` when it cannot be trusted."""
    body_keys = _MARKER.findall(str(issue.get("body") or ""))
    title = _TITLE.search(str(issue.get("title") or ""))
    if not body_keys:
        return "", "no ci-incident marker in the body"
    if title is None:
        return "", "the title is not a CI incident title"
    if len(set(body_keys)) != 1 or body_keys[0] != title.group(1):
        return "", f"the marker ({', '.join(sorted(set(body_keys)))}) does not match the title ({title.group(1)})"
    return body_keys[0], ""


def _persons(client: CIClient, repo: str, number: int, comments: list[dict]) -> list[str]:
    out: list[str] = []
    for row in comments:
        user = row.get("user")
        if _is_bot(user):
            continue
        login = str((user or {}).get("login") or "someone") if isinstance(user, dict) else "someone"
        stamp = str(row.get("created_at") or "")[:10]
        text = f"@{login}" + (f" on {stamp}" if stamp else "")
        if text not in out:
            out.append(text)
    return out


def _newest_occurrence(issue: dict, comments: list[dict]) -> str:
    shas = _OCCURRENCE.findall(str(issue.get("body") or ""))
    for row in comments:
        shas += _OCCURRENCE.findall(str(row.get("body") or ""))
    if shas:
        return shas[-1]
    first = _FIRST.search(str(issue.get("title") or ""))
    return first.group(1) if first else ""


def _green_main_runs(client: CIClient, config: CIHealthConfig, repo: str) -> list[dict]:
    rows = client.workflow_runs(repo, config.main_workflow, config.default_branch, per_page=100)
    greens = [
        r
        for r in rows
        if str(r.get("event") or "") == "push"
        and str(r.get("status") or "") == "completed"
        and str(r.get("conclusion") or "") == "success"
        and str(r.get("head_branch") or config.default_branch) == config.default_branch
    ]
    return sorted(greens, key=lambda r: str(r.get("created_at") or ""))


def _judge_main(client, config, repo, issue, comments, finding, greens) -> None:
    occurrence = _newest_occurrence(issue, comments)
    if not occurrence:
        finding.verdict, finding.reason = KEEP, "no occurrence commit is named in the incident"
        return
    try:
        full = str(client.commit(repo, occurrence).get("sha") or "")
    except CIClientError as exc:
        finding.verdict, finding.reason = KEEP, f"the occurrence commit {occurrence} could not be read ({exc})"
        return
    if not greens:
        finding.verdict, finding.reason = KEEP, "no completed green run of the main workflow was read"
        return
    newest = greens[-1]
    sha = str(newest.get("head_sha") or "")
    if not full or not sha or sha == full or not client.is_ancestor(repo, full, sha):
        finding.verdict = KEEP
        finding.reason = (
            f"no completed green run on a later commit than {full[:10] or occurrence}: "
            f"the newest green run is on {sha[:10]}"
        )
        return
    run_id = newest.get("id")
    finding.verdict, finding.later_sha, finding.later_run = CLOSE, sha, str(run_id)
    finding.reason = f"{config.main_workflow} run {run_id} is green on {sha[:10]}, a later commit than {full[:10]}"
    finding.comment = (
        f"The cause is gone: `{config.default_branch}` has a completed green `{config.main_workflow}` run on a later "
        f"commit than this incident's newest occurrence (`{full[:10]}`): run {run_id} on `{sha[:10]}` "
        f"(https://github.com/{repo}/actions/runs/{run_id}). Closing this incident; a new failure opens a new one."
    )


def _judge_semgrep(client, config, repo, issue, comments, finding) -> None:
    stamps = _AT.findall(str(issue.get("body") or ""))
    for row in comments:
        stamps += _AT.findall(str(row.get("body") or ""))
    since = max(stamps) if stamps else str(issue.get("created_at") or "")
    if not since:
        finding.verdict, finding.reason = KEEP, "no occurrence time is named in the incident"
        return
    rows = client.workflow_runs(repo, config.semgrep_workflow, config.default_branch, per_page=30)
    done = sorted(
        (
            r
            for r in rows
            if str(r.get("status") or "") == "completed"
            and str(r.get("conclusion") or "") in ("success", "failure")
            and str(r.get("event") or "") in ("push", "schedule", "workflow_dispatch")
        ),
        key=lambda r: str(r.get("created_at") or ""),
    )
    if not done:
        finding.verdict, finding.reason = KEEP, "no completed Semgrep run on the default branch was read"
        return
    newest = done[-1]
    if str(newest.get("conclusion")) != "success" or str(newest.get("created_at") or "") <= since:
        finding.verdict = KEEP
        finding.reason = f"the newest completed Semgrep run is {newest.get('conclusion')} and not later than {since}"
        return
    run_id, sha = newest.get("id"), str(newest.get("head_sha") or "")
    finding.verdict, finding.later_sha, finding.later_run = CLOSE, sha, str(run_id)
    finding.reason = f"{config.semgrep_workflow} run {run_id} is green on {sha[:10]}, after the newest occurrence ({since})"
    finding.comment = (
        f"The cause is gone: the newest completed `{config.semgrep_workflow}` run on `{config.default_branch}` is green "
        f"and started after this incident's newest occurrence ({since}): run {run_id} on `{sha[:10]}` "
        f"(https://github.com/{repo}/actions/runs/{run_id}). Closing this incident; a new failure opens a new one."
    )


def _publish_success(client, config, repo, tag, cache: dict) -> dict | None:
    if tag not in cache:
        try:
            rows = client.workflow_runs(repo, config.publish_workflow, tag, per_page=10)
        except CIClientError:
            rows = []
        cache[tag] = next(
            (
                r
                for r in rows
                if str(r.get("status") or "") == "completed"
                and str(r.get("conclusion") or "") == "success"
                and str(r.get("head_branch") or tag) == tag
            ),
            None,
        )
    return cache[tag]


def _judge_tag(client, config, repo, key, finding, later_tags: list[str], cache: dict) -> None:
    mine = _version(key)
    later = sorted((t for t in later_tags if _version(t) and mine and _version(t) > mine), key=_version, reverse=True)
    if not later:
        finding.verdict, finding.reason = KEEP, f"no later version tag than {key} exists"
        return
    for tag in later[:MAX_TAGS_ASKED]:
        run = _publish_success(client, config, repo, tag, cache)
        if run is not None:
            sha = str(run.get("head_sha") or "")
            finding.verdict, finding.later_sha, finding.later_run = CLOSE, sha, str(run.get("id"))
            finding.reason = f"{tag} was published by run {run.get('id')}, a later tag than {key}"
            finding.comment = (
                f"The cause is gone: a later tag, `{tag}`, has a completed successful publish run "
                f"({config.publish_workflow} run {run.get('id')} on `{sha[:10]}`, "
                f"https://github.com/{repo}/actions/runs/{run.get('id')}), so `{key}` is superseded. "
                "Closing this incident; a new failure opens a new one."
            )
            return
    finding.verdict = KEEP
    finding.reason = f"none of the {min(len(later), MAX_TAGS_ASKED)} newest later tags has a successful publish run"


def scan(client: CIClient, config: CIHealthConfig, repo: str) -> list[Finding]:
    """Judge every open CI incident. Reads only."""
    findings: list[Finding] = []
    issues = sorted(client.open_issues(repo, config.incident_labels[0]), key=lambda i: int(i.get("number") or 0))
    greens: list[dict] | None = None
    later_tags: list[str] | None = None
    publish_cache: dict = {}
    for issue in issues:
        number = int(issue.get("number") or 0)
        finding = Finding(number=number, title=str(issue.get("title") or ""))
        findings.append(finding)
        if not _MARKER.search(str(issue.get("body") or "")) and not _TITLE.search(finding.title):
            finding.reason = "not a CI incident"
            continue
        key, why = _key(issue)
        finding.key = key
        if not key:
            finding.reason = why
            continue
        if not _is_bot(issue.get("user")):
            finding.reason = "a person filed this issue; it is theirs to close"
            continue
        is_main = key == config.default_branch
        is_semgrep = key == SEMGREP_KEY
        if not is_main and not is_semgrep and _version(key) is None:
            finding.reason = f"the key {key} is neither {config.default_branch} nor a version tag"
            continue
        try:
            comments = client.issue_comments(repo, number)
            if is_semgrep:
                _judge_semgrep(client, config, repo, issue, comments, finding)
            elif is_main:
                if greens is None:
                    greens = _green_main_runs(client, config, repo)
                _judge_main(client, config, repo, issue, comments, finding, greens)
            else:
                if later_tags is None:
                    later_tags = list(client.tags(repo))
                _judge_tag(client, config, repo, key, finding, later_tags, publish_cache)
        except CIClientError as exc:
            finding.verdict, finding.reason = KEEP, f"the forge could not be read for this incident ({exc})"
            continue
        if finding.verdict == CLOSE:
            finding.persons = _persons(client, repo, number, comments)
            if finding.persons:
                finding.comment += (
                    f" A person commented on this incident ({', '.join(finding.persons)}); "
                    "it is closed because its cause is gone, not because that comment was answered. "
                    "Reopen it if that comment still needs an answer."
                )
    return findings


def close(client: CIClient, repo: str, findings: list[Finding]) -> list[int]:
    """Comment, then close, each finding that says ``close``. Returns the numbers closed."""
    closed: list[int] = []
    for finding in findings:
        if finding.verdict != CLOSE or not finding.comment:
            continue
        client.comment_issue(repo, finding.number, finding.comment)
        client.close_issue(repo, finding.number)
        closed.append(finding.number)
    return closed


def render(findings: list[Finding], *, act: bool) -> str:
    lines = []
    for f in findings:
        if f.verdict == CLOSE:
            lines.append(f"{'closed' if act else 'would close'} #{f.number} ({f.key}): {f.reason}")
        else:
            lines.append(f"{f.verdict} #{f.number} ({f.key or 'no key'}): {f.reason}")
    closing = sum(1 for f in findings if f.verdict == CLOSE)
    tail = f"{closing} of {len(findings)} open incident(s) {'closed' if act else 'would close'}"
    if not act:
        tail += "; nothing was closed"
    return "\n".join([*lines, tail]) + "\n"
