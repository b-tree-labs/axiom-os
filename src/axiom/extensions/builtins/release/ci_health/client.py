# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The forge reads and the one write CI-health needs, behind one seam.

Every detector takes a ``CIClient``. Tests hand in fixture responses and no
network is touched. ``GhClient`` is the real one: plain ``gh api`` reads, and
``cancel_run``, ``draft_revert`` and the incident calls as its only writes.

Billing reads use ``/organizations/<org>/settings/billing/usage``. The older
``/orgs/<org>/settings/billing/actions`` answers 410 and is not called.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Protocol
from urllib.parse import quote


class CIClientError(RuntimeError):
    """A read failed. The caller reports it and does not guess."""


class CIClient(Protocol):
    def billing_usage(self, org: str, year: int, month: int) -> list[dict]: ...

    def budgets(self, org: str) -> list[dict]: ...

    def runs(self, repo: str, *, created_since: str, max_pages: int = 10) -> list[dict]: ...

    def jobs(self, repo: str, run_id: int) -> list[dict]: ...

    def job_log(self, repo: str, job_id: int) -> str: ...

    def is_ancestor(self, repo: str, older_sha: str, newer_sha: str) -> bool: ...

    def branch_protection(self, repo: str, branch: str) -> dict | None: ...

    def branch_rules(self, repo: str, branch: str) -> list[dict]: ...

    def cancel_run(self, repo: str, run_id: int) -> None: ...

    def merged_pulls(self, repo: str, *, since: str, base: str) -> list[dict]: ...

    def pull_files(self, repo: str, number: int) -> list[str]: ...

    def draft_revert(self, node_id: str, title: str, body: str) -> str: ...

    def workflow_runs(self, repo: str, workflow: str, branch: str, *, per_page: int = 30) -> list[dict]: ...

    def branch_head(self, repo: str, branch: str) -> str: ...

    def workflow_files(self, repo: str, ref: str) -> dict[str, str]: ...

    def open_issues(self, repo: str, label: str) -> list[dict]: ...

    def create_issue(self, repo: str, title: str, body: str, *, labels, assignees) -> dict: ...

    def edit_issue(self, repo: str, number: int, *, body: str) -> None: ...

    def comment_issue(self, repo: str, number: int, body: str) -> None: ...

    def close_issue(self, repo: str, number: int) -> None: ...


Runner = Any  # callable(list[str]) -> str


def _default_runner(args: list[str]) -> str:
    try:
        done = subprocess.run(
            ["gh", "api", *args],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CIClientError(f"gh api {args[0] if args else ''}: {exc}") from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        raise CIClientError(f"gh api {args[0] if args else ''}: {detail[-1] if detail else done.returncode}")
    return done.stdout


class GhClient:
    """``gh api`` reads, plus ``cancel_run``, ``draft_revert`` and incident writes.

    ``draft_revert`` opens a draft pull request. Nothing here merges anything.
    """

    def __init__(self, runner: Runner | None = None) -> None:
        self._run = runner or _default_runner

    def _json(self, args: list[str]) -> Any:
        raw = self._run(args)
        try:
            return json.loads(raw or "null")
        except json.JSONDecodeError as exc:
            raise CIClientError(f"gh api {args[0]}: not JSON") from exc

    def billing_usage(self, org: str, year: int, month: int) -> list[dict]:
        data = self._json([f"/organizations/{org}/settings/billing/usage?year={year}&month={month}"])
        items = data.get("usageItems") if isinstance(data, dict) else None
        return [item for item in items or [] if isinstance(item, dict)]

    def budgets(self, org: str) -> list[dict]:
        try:
            data = self._json([f"/organizations/{org}/settings/billing/budgets"])
        except CIClientError:
            return []
        rows = data.get("budgets") if isinstance(data, dict) else None
        return [row for row in rows or [] if isinstance(row, dict)]

    def runs(self, repo: str, *, created_since: str, max_pages: int = 10) -> list[dict]:
        rows: list[dict] = []
        for page in range(1, max_pages + 1):
            data = self._json(
                [f"repos/{repo}/actions/runs?per_page=100&page={page}&created=%3E%3D{created_since}"]
            )
            batch = data.get("workflow_runs") if isinstance(data, dict) else None
            if not batch:
                break
            rows.extend(row for row in batch if isinstance(row, dict))
            if len(batch) < 100:
                break
        return rows

    def jobs(self, repo: str, run_id: int) -> list[dict]:
        data = self._json([f"repos/{repo}/actions/runs/{int(run_id)}/jobs?per_page=100"])
        rows = data.get("jobs") if isinstance(data, dict) else None
        return [row for row in rows or [] if isinstance(row, dict)]

    def job_log(self, repo: str, job_id: int) -> str:
        try:
            return self._run([f"repos/{repo}/actions/jobs/{int(job_id)}/logs"])
        except CIClientError:
            return ""

    def is_ancestor(self, repo: str, older_sha: str, newer_sha: str) -> bool:
        try:
            data = self._json([f"repos/{repo}/compare/{older_sha}...{newer_sha}"])
        except CIClientError:
            return False
        return isinstance(data, dict) and data.get("status") in {"ahead", "identical"}

    def branch_protection(self, repo: str, branch: str) -> dict | None:
        try:
            data = self._json([f"repos/{repo}/branches/{branch}/protection"])
        except CIClientError:
            return None
        return data if isinstance(data, dict) else None

    def branch_rules(self, repo: str, branch: str) -> list[dict]:
        try:
            data = self._json([f"repos/{repo}/rules/branches/{branch}"])
        except CIClientError:
            return []
        return [row for row in data or [] if isinstance(row, dict)] if isinstance(data, list) else []

    def cancel_run(self, repo: str, run_id: int) -> None:
        self._run(["-X", "POST", f"repos/{repo}/actions/runs/{int(run_id)}/cancel"])

    def merged_pulls(self, repo: str, *, since: str, base: str) -> list[dict]:
        """Pull requests merged into ``base`` since the ``since`` date, newest first."""
        rows: list[dict] = []
        for page in range(1, 11):
            data = self._json(
                [f"repos/{repo}/pulls?state=closed&base={base}&sort=updated&direction=desc&per_page=100&page={page}"]
            )
            if not isinstance(data, list) or not data:
                break
            rows.extend(r for r in data if isinstance(r, dict) and r.get("merged_at"))
            if len(data) < 100 or str(data[-1].get("updated_at") or "")[:10] < since:
                break
        return [r for r in rows if str(r.get("merged_at"))[:10] >= since]

    def pull_files(self, repo: str, number: int) -> list[str]:
        names: list[str] = []
        for page in range(1, 11):
            data = self._json([f"repos/{repo}/pulls/{int(number)}/files?per_page=100&page={page}"])
            if not isinstance(data, list) or not data:
                break
            names.extend(str(r.get("filename") or "") for r in data if isinstance(r, dict))
            if len(data) < 100:
                break
        return [n for n in names if n]

    def workflow_runs(self, repo: str, workflow: str, branch: str, *, per_page: int = 30) -> list[dict]:
        """The newest runs of one workflow file on one branch."""
        data = self._json(
            [f"repos/{repo}/actions/workflows/{workflow}/runs?branch={quote(branch)}&per_page={int(per_page)}"]
        )
        rows = data.get("workflow_runs") if isinstance(data, dict) else None
        return [row for row in rows or [] if isinstance(row, dict)]

    def branch_head(self, repo: str, branch: str) -> str:
        data = self._json([f"repos/{repo}/branches/{quote(branch)}"])
        commit = data.get("commit") if isinstance(data, dict) else None
        return str(commit.get("sha") or "") if isinstance(commit, dict) else ""

    def workflow_files(self, repo: str, ref: str) -> dict[str, str]:
        """Workflow file names and text at ``ref``, read from the forge."""
        rows = self._json([f"repos/{repo}/contents/.github/workflows?ref={quote(ref)}"])
        out: dict[str, str] = {}
        for row in rows if isinstance(rows, list) else []:
            name = str(row.get("name") or "") if isinstance(row, dict) else ""
            if name.endswith((".yml", ".yaml")):
                out[name] = self._run(
                    [f"repos/{repo}/contents/{row['path']}?ref={quote(ref)}", "-H", "Accept: application/vnd.github.raw+json"]
                )
        return out

    def open_issues(self, repo: str, label: str) -> list[dict]:
        data = self._json([f"repos/{repo}/issues?state=open&labels={quote(label)}&per_page=100"])
        return [r for r in data or [] if isinstance(r, dict) and "pull_request" not in r] if isinstance(data, list) else []

    def create_issue(self, repo: str, title: str, body: str, *, labels, assignees) -> dict:
        args = ["-X", "POST", f"repos/{repo}/issues", "-f", f"title={title}", "-f", f"body={body}"]
        args += [item for label in labels for item in ("-f", f"labels[]={label}")]
        args += [item for login in assignees for item in ("-f", f"assignees[]={login}")]
        data = self._json(args)
        return data if isinstance(data, dict) else {}

    def edit_issue(self, repo: str, number: int, *, body: str) -> None:
        self._run(["-X", "PATCH", f"repos/{repo}/issues/{int(number)}", "-f", f"body={body}"])

    def comment_issue(self, repo: str, number: int, body: str) -> None:
        self._run(["-X", "POST", f"repos/{repo}/issues/{int(number)}/comments", "-f", f"body={body}"])

    def close_issue(self, repo: str, number: int) -> None:
        self._run(["-X", "PATCH", f"repos/{repo}/issues/{int(number)}", "-f", "state=closed"])

    def draft_revert(self, node_id: str, title: str, body: str) -> str:
        """Open a draft revert pull request. Returns its URL."""
        query = (
            "mutation($id:ID!,$title:String!,$body:String!){revertPullRequest("
            "input:{pullRequestId:$id,title:$title,body:$body,draft:true})"
            "{revertPullRequest{url}}}"
        )
        data = self._json(["graphql", "-f", f"query={query}", "-f", f"id={node_id}", "-f", f"title={title}", "-f", f"body={body}"])
        try:
            return str(data["data"]["revertPullRequest"]["revertPullRequest"]["url"])
        except (KeyError, TypeError) as exc:
            raise CIClientError("gh api graphql: revert not created") from exc
