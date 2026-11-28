# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A read-only GitHub adapter for the landing queue (#2399, slice 2).

Reads only, through ``gh api`` with GET: open pull requests, the base tip, the merge
commit GitHub computes for the merged state, and workflow runs by sha. It uses the same
runner CI-health uses. Any call that is not a plain GET is refused before it reaches
``gh``, and the write methods of ``QueueAdapter`` raise. A value that is missing, of the
wrong type or not a full sha is ``QueueReadError``, never a guess.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import quote

from axiom.extensions.builtins.release.ci_health.client import CIClientError, _default_runner
from axiom.extensions.builtins.release.lander.queue import Candidate
from axiom.extensions.builtins.release.lander.queue_adapter import (
    CheckOutcome,
    CiResult,
    PullFacts,
    QueueReadError,
    QueueWriteRefused,
)

_SHA = re.compile(r"^[0-9a-f]{40}$")
_WRITE_FLAGS = {"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"}
_FAILED = {"failure", "cancelled", "timed_out", "startup_failure", "action_required"}


def _sha(value: object, what: str) -> str:
    text = value if isinstance(value, str) else ""
    if not _SHA.match(text):
        raise QueueReadError(f"{what} is not a full sha")
    return text


def _minutes(run: dict) -> float:
    try:
        start = datetime.fromisoformat(
            str(run["run_started_at"] or run["created_at"]).replace("Z", "+00:00")
        )
        end = datetime.fromisoformat(str(run["updated_at"]).replace("Z", "+00:00"))
    except (KeyError, ValueError, TypeError):
        return 0.0
    return max((end - start).total_seconds() / 60.0, 0.0)


def checks_state(rows: object) -> str:
    """``passing``, ``failing``, ``pending`` or ``none`` from check-run rows."""
    if not isinstance(rows, list):
        raise QueueReadError("check runs were not a list")
    if not rows:
        return "none"
    pending = False
    for row in rows:
        if not isinstance(row, dict):
            raise QueueReadError("a check run was not a mapping")
        if row.get("status") != "completed":
            pending = True
        elif str(row.get("conclusion") or "") in _FAILED:
            return "failing"
        elif str(row.get("conclusion") or "") not in {"success", "neutral", "skipped"}:
            raise QueueReadError("a check run has an unknown conclusion")
    return "pending" if pending else "passing"


class GitHubQueueReader:
    """Implements ``QueueReader`` and, with every write refused, ``QueueAdapter``."""

    name = "github"

    def __init__(self, runner: Callable[[list[str]], str] | None = None) -> None:
        self._runner = runner or _default_runner

    # -- the one door to the network ----------------------------------------------

    def _get(self, path: str) -> Any:
        args = [path]
        if any(arg in _WRITE_FLAGS for arg in args) or not path.startswith("repos/"):
            raise QueueWriteRefused("the queue reader only issues GET reads of repos/")
        try:
            raw = self._runner(args)
        except CIClientError as exc:
            raise QueueReadError(str(exc)) from exc
        try:
            return json.loads(raw or "null")
        except json.JSONDecodeError as exc:
            raise QueueReadError(f"{path}: not JSON") from exc

    # -- reads --------------------------------------------------------------------

    def base_sha(self, repo: str, base: str) -> str:
        data = self._get(f"repos/{repo}/branches/{quote(base)}")
        commit = data.get("commit") if isinstance(data, dict) else None
        return _sha(commit.get("sha") if isinstance(commit, dict) else None, "base tip")

    def _facts(self, repo: str, row: object) -> PullFacts:
        if not isinstance(row, dict):
            raise QueueReadError("pull request was not a mapping")
        head, base = row.get("head"), row.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise QueueReadError("pull request has no head or base")
        number = row.get("number")
        if isinstance(number, bool) or not isinstance(number, int):
            raise QueueReadError("pull request has no number")
        head_sha = _sha(head.get("sha"), "head")
        labels = row.get("labels")
        if not isinstance(labels, list):
            raise QueueReadError("labels were not a list")
        mergeable = row.get("mergeable")
        if mergeable is not None and not isinstance(mergeable, bool):
            raise QueueReadError("mergeable was not true, false or null")
        merge_sha = row.get("merge_commit_sha")
        # GitHub's merge commit is only meaningful when it says the pull is mergeable.
        merged_state = _sha(merge_sha, "merge commit") if mergeable is True else ""
        return PullFacts(
            number=number,
            head_sha=head_sha,
            base_ref=str(base.get("ref") or ""),
            labels=tuple(str(item.get("name") or "") for item in labels if isinstance(item, dict)),
            checks=self._checks(repo, head_sha),
            mergeable=mergeable,
            merge_commit_sha=merged_state,
            base_sha=_sha(base.get("sha"), "base"),
        )

    def _checks(self, repo: str, sha: str) -> str:
        data = self._get(f"repos/{repo}/commits/{sha}/check-runs?per_page=100")
        rows = data.get("check_runs") if isinstance(data, dict) else None
        return checks_state(rows)

    def pull_facts(self, repo: str, number: int) -> PullFacts:
        return self._facts(repo, self._get(f"repos/{repo}/pulls/{int(number)}"))

    def open_pulls(self, repo: str) -> list[PullFacts]:
        rows = self._get(f"repos/{repo}/pulls?state=open&per_page=100")
        if not isinstance(rows, list):
            raise QueueReadError("pull request list was not a list")
        out = []
        for row in rows:
            number = row.get("number") if isinstance(row, dict) else None
            if isinstance(number, bool) or not isinstance(number, int):
                raise QueueReadError("pull request has no number")
            # The list omits mergeable and the merge commit: read each one.
            out.append(self.pull_facts(repo, number))
        return out

    def ci_results(self, repo: str, sha: str) -> CiResult:
        _sha(sha, "ci sha")
        data = self._get(f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100")
        rows = data.get("workflow_runs") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise QueueReadError("workflow runs were not a list")
        if not rows:
            return CiResult(sha, "none")
        if any(not isinstance(r, dict) for r in rows):
            raise QueueReadError("a workflow run was not a mapping")
        minutes = round(sum(_minutes(r) for r in rows), 2)
        if any(r.get("status") != "completed" for r in rows):
            state = "pending"
        elif any(str(r.get("conclusion") or "") in _FAILED for r in rows):
            state = "failure"
        else:
            state = "success"
        return CiResult(sha, state, runs=len(rows), minutes=minutes)

    # -- QueueAdapter: reads answer, writes are refused ------------------------------

    def wait_for_checks(self, branch: str) -> CheckOutcome:
        raise QueueWriteRefused("a read-only adapter does not wait on a queue branch it never made")

    def build_candidate(self, candidate: Candidate, ahead: tuple[int, ...]) -> str:
        raise QueueWriteRefused("the read-only adapter creates no branch")

    def land(self, candidate: Candidate) -> str:
        raise QueueWriteRefused("the read-only adapter merges nothing")

    def delete_branch(self, branch: str) -> None:
        raise QueueWriteRefused("the read-only adapter deletes nothing")
