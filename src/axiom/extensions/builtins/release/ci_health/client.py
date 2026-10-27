# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The forge reads and the one write CI-health needs, behind one seam.

Every detector takes a ``CIClient``. Tests hand in fixture responses and no
network is touched. ``GhClient`` is the real one: plain ``gh api`` reads, and
``cancel_run`` as its only write.

Billing reads use ``/organizations/<org>/settings/billing/usage``. The older
``/orgs/<org>/settings/billing/actions`` answers 410 and is not called.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Protocol


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
    """``gh api`` reads. ``cancel_run`` is the only write."""

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
