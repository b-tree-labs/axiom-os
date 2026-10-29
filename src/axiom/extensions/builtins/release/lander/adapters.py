# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Forge and CI adapters.

GitHub is implemented with the ``gh`` CLI. The token stays in the host
environment (``GH_TOKEN`` or ``GITHUB_TOKEN``); it is never placed on
the command line. GitLab is a stub: merge trains are named and not wired.
A CI adapter reports observed suite minutes; it does not start runs.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from typing import Any, Protocol

from axiom.extensions.builtins.release.actions_spend import learn_suite_minutes
from axiom.extensions.builtins.release.lander.model import FailingCheck, OpenPull, parse_time
from axiom.extensions.builtins.release.lander.reviews import review_fields
from axiom.extensions.builtins.release.lander.status import advisory_status, refuse_status_post
from axiom.extensions.builtins.release.lander.tickets import TicketRouter

_LIST_FIELDS = ",".join(
    (
        "number",
        "title",
        "headRefName",
        "headRefOid",
        "baseRefName",
        "author",
        "labels",
        "updatedAt",
        "createdAt",
        "isDraft",
        "mergeable",
        "mergeStateStatus",
        "url",
        "body",
        "additions",
        "deletions",
    )
)


class ForgeUnavailable(RuntimeError):
    """The forge could not be read, or the adapter is only a stub."""


class Forge(Protocol):
    name: str

    def list_open(self, repo: str) -> list[OpenPull]:
        """Open pull requests. Raises ForgeUnavailable when it cannot."""

    def suite_samples(self, repo: str) -> list[int]:
        """Observed suite durations in minutes. Empty when unknown."""


class CIAdapter(Protocol):
    name: str

    def suite_minutes(self, repo: str) -> int:
        """A single suite cost for ``repo``."""


def _label_names(raw: object) -> tuple[str, ...]:
    names: list[str] = []
    if not isinstance(raw, list):
        return ()
    for item in raw:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and item.get("name"):
            names.append(str(item["name"]))
    return tuple(names)


def _author(raw: object) -> str:
    if isinstance(raw, dict):
        return str(raw.get("login") or "")
    return str(raw or "")


def _files(raw: object) -> tuple[str, ...]:
    paths: list[str] = []
    if not isinstance(raw, list):
        return ()
    for item in raw:
        if isinstance(item, str):
            paths.append(item)
        elif isinstance(item, dict):
            path = item.get("path") or item.get("filename")
            if path:
                paths.append(str(path))
    return tuple(sorted(set(paths)))


def _mergeable(raw: object) -> bool | None:
    if isinstance(raw, bool) or raw is None:
        return raw
    token = str(raw).upper()
    return {"MERGEABLE": True, "CONFLICTING": False}.get(token)


_FAILED = {
    "FAILURE",
    "CANCELLED",
    "TIMED_OUT",
    "ACTION_REQUIRED",
    "STARTUP_FAILURE",
    "ERROR",
}


def failing_checks_from_rollup(items: object) -> tuple[FailingCheck, ...]:
    """Names and log URLs of failing checks. Empty when the rollup has none."""
    if not isinstance(items, list):
        return ()
    found: list[FailingCheck] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        conclusion = str(item.get("conclusion") or item.get("state") or "").upper()
        if conclusion not in _FAILED:
            continue
        name = str(item.get("name") or item.get("context") or "")
        if not name:
            continue
        url = str(item.get("detailsUrl") or item.get("targetUrl") or item.get("url") or "")
        found.append(FailingCheck(name, url))
    return tuple(found)


def checks_from_rollup(items: object) -> str:
    """Reduce a check rollup to passing, failing, pending, or unknown."""
    if not isinstance(items, list) or not items:
        return "unknown"
    states: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        conclusion = str(item.get("conclusion") or item.get("state") or "").upper()
        status = str(item.get("status") or "").upper()
        if status in {"QUEUED", "IN_PROGRESS", "PENDING", "WAITING"} or conclusion in {
            "PENDING",
            "QUEUED",
        }:
            states.append("pending")
        elif conclusion in {
            "FAILURE",
            "CANCELLED",
            "TIMED_OUT",
            "ACTION_REQUIRED",
            "STARTUP_FAILURE",
            "ERROR",
        }:
            states.append("failing")
        elif conclusion in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
            states.append("passing")
        else:
            states.append("unknown")
    if any(state == "failing" for state in states):
        return "failing"
    if any(state == "pending" for state in states):
        return "pending"
    if states and all(state == "passing" for state in states):
        return "passing"
    return "unknown"


def parse_pr(row: dict[str, Any], *, self_landers: tuple[str, ...] = ()) -> OpenPull:
    """One ``gh`` JSON object, plus optional ``files`` and a rollup."""
    checks = row.get("checks")
    if not isinstance(checks, str) or not checks:
        checks = checks_from_rollup(row.get("statusCheckRollup"))
    pull = OpenPull(
        number=int(row["number"]),
        title=str(row.get("title") or ""),
        head_sha=str(row.get("headRefOid") or row.get("head_sha") or ""),
        head_ref=str(row.get("headRefName") or row.get("head_ref") or ""),
        base_ref=str(row.get("baseRefName") or row.get("base_ref") or "main"),
        author=_author(row.get("author")),
        labels=_label_names(row.get("labels")),
        files=_files(row.get("files")),
        updated_at=parse_time(row.get("updatedAt") or row.get("updated_at")),
        created_at=parse_time(row.get("createdAt") or row.get("created_at")),
        draft=bool(row.get("isDraft") or row.get("draft") or False),
        mergeable=_mergeable(row.get("mergeable")),
        checks=checks,
        ci_attempts=int(row.get("ci_attempts") or 0),
        additions=int(row.get("additions") or 0),
        deletions=int(row.get("deletions") or 0),
        url=str(row.get("url") or ""),
        body=str(row.get("body") or ""),
        failure_log=str(row.get("failure_log") or ""),
        author_model=str(row.get("author_model") or ""),
        merge_state=str(row.get("mergeStateStatus") or row.get("merge_state") or "").upper(),
        failing_checks=failing_checks_from_rollup(row.get("statusCheckRollup"))
        or tuple(
            FailingCheck(str(item.get("name") or ""), str(item.get("log_url") or ""))
            for item in row.get("failing_checks") or []
            if isinstance(item, dict) and item.get("name")
        ),
    )
    pending, changes = review_fields(row, self_landers=self_landers)
    if row.get("pending_human_reviewers"):
        pending = tuple(str(item) for item in row["pending_human_reviewers"])
    if row.get("changes_requested_by"):
        changes = tuple(str(item) for item in row["changes_requested_by"])
    return replace(pull, pending_human_reviewers=pending, changes_requested_by=changes)


def minutes_between(started: object, updated: object) -> int | None:
    if not isinstance(started, str) or not isinstance(updated, str):
        return None
    try:
        start = parse_time(started)
        end = parse_time(updated)
    except ValueError:
        return None
    seconds = (end - start).total_seconds()
    if seconds <= 0:
        return None
    return max(1, int(round(seconds / 60)))


def suite_minutes_from_runs(rows: object) -> list[int]:
    """Successful pull-request runs, in minutes, from ``gh run list`` rows."""
    if not isinstance(rows, list):
        return []
    samples: list[int] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("event") or "") not in {"", "pull_request"}:
            continue
        if str(row.get("conclusion") or "").lower() not in {"success", ""}:
            continue
        if str(row.get("status") or "completed").lower() not in {"completed", ""}:
            continue
        minutes = minutes_between(row.get("startedAt"), row.get("updatedAt"))
        if minutes is not None:
            samples.append(minutes)
    return samples


def _redact(text: str) -> str:
    redacted = text
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "GITLAB_TOKEN"):
        secret = os.environ.get(name) or ""
        if secret:
            redacted = redacted.replace(secret, "[redacted]")
    return redacted[:500]


def default_gh(args: list[str]) -> str:
    """Run ``gh``. The token is inherited from the environment, not argv.

    A failure names the command and the forge's own error. The type name
    alone is not enough to see which read was refused.
    """
    shown = " ".join(args)
    try:
        done = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ForgeUnavailable(f"gh {shown} failed: {exc}") from exc
    if done.returncode != 0:
        detail = _redact((done.stderr or done.stdout or "gh failed").strip())
        raise ForgeUnavailable(f"gh {shown} failed: {detail or 'gh failed'}")
    return done.stdout


class GitHubForge:
    """Read open pull requests and observed Actions minutes. Writes nothing."""

    name = "github"

    def __init__(self, runner=None, *, limit: int = 100, self_landers: tuple[str, ...] = ()) -> None:
        self._runner = runner or default_gh
        self.limit = limit
        self.self_landers = tuple(self_landers)

    def list_open(self, repo: str) -> list[OpenPull]:
        raw = self._runner(
            [
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--limit",
                str(self.limit),
                "--json",
                _LIST_FIELDS,
            ]
        )
        try:
            rows = json.loads(raw or "[]")
        except json.JSONDecodeError as exc:
            raise ForgeUnavailable("pull request list was not JSON") from exc
        if not isinstance(rows, list):
            raise ForgeUnavailable("pull request list was not a list")
        router = TicketRouter()
        pulls: list[OpenPull] = []
        for row in rows:
            if not isinstance(row, dict) or "number" not in row:
                continue
            detail_raw = self._runner(
                [
                    "pr",
                    "view",
                    str(row["number"]),
                    "--repo",
                    repo,
                    "--json",
                    "files,statusCheckRollup,reviewRequests,reviews,latestReviews",
                ]
            )
            try:
                detail = json.loads(detail_raw or "{}")
            except json.JSONDecodeError:
                detail = {}
            if isinstance(detail, dict):
                row = {**row, **detail}
            pull = parse_pr(row, self_landers=self.self_landers)
            pulls.append(replace(pull, ticket_ids=router.ids_for(pull)))
        return pulls

    def suite_samples(self, repo: str) -> list[int]:
        raw = self._runner(
            [
                "run",
                "list",
                "--repo",
                repo,
                "--limit",
                "30",
                "--json",
                "status,conclusion,startedAt,updatedAt,event",
            ]
        )
        try:
            rows = json.loads(raw or "[]")
        except json.JSONDecodeError:
            return []
        return suite_minutes_from_runs(rows)

    def post_status(self, repo: str, sha: str, batch_key: str, members: list[int]) -> None:
        """Build the advisory payload, then refuse to post it."""
        del repo, sha
        refuse_status_post(advisory_status(batch_key, members))


class GitLabForge:
    """Stub. GitLab merge trains and pipeline status are not wired.

    The class exists so a config file can name ``gitlab`` and fail with
    a documented reason instead of a missing import. Secrets would come
    from ``GITLAB_TOKEN`` in the host environment, the same way GitHub
    reads ``GH_TOKEN``. Nothing here reads that variable.
    """

    name = "gitlab"

    def list_open(self, repo: str) -> list[OpenPull]:
        raise ForgeUnavailable(
            "GitLab forge adapter is a stub. Merge trains are not wired. "
            f"Use forge='github' to read {repo}, or pass pull requests in memory."
        )

    def suite_samples(self, repo: str) -> list[int]:
        raise ForgeUnavailable(
            "GitLab CI adapter is a stub. Pipeline minutes are not read."
        )

    def post_status(self, repo: str, sha: str, batch_key: str, members: list[int]) -> None:
        del repo, sha
        refuse_status_post(advisory_status(batch_key, members))


class StaticCI:
    """A suite cost the caller already learned or configured."""

    name = "static"

    def __init__(self, minutes: int) -> None:
        self._minutes = minutes

    def suite_minutes(self, repo: str) -> int:
        del repo
        return learn_suite_minutes([self._minutes])
