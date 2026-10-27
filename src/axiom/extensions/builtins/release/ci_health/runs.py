# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Workflow runs, normalised once, so every detector reads the same shape."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from axiom.extensions.builtins.release.lander.model import parse_time

ACTIVE = frozenset({"queued", "in_progress", "waiting", "requested", "pending"})
FAILED = frozenset({"failure", "cancelled", "timed_out", "startup_failure"})


@dataclass(frozen=True)
class Run:
    id: int
    repo: str
    workflow: str
    workflow_id: int
    path: str
    event: str
    status: str
    conclusion: str
    head_branch: str
    head_sha: str
    run_attempt: int
    created_at: datetime
    started_at: datetime
    updated_at: datetime
    message: str
    pr_numbers: tuple[int, ...]
    minutes: float

    @property
    def active(self) -> bool:
        return self.status in ACTIVE

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repo}/actions/runs/{self.id}"


def _int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def parse_run(row: dict, repo: str, *, now: datetime | None = None) -> Run:
    now = now or datetime.now(UTC)
    created = parse_time(row.get("created_at"))
    started = parse_time(row.get("run_started_at") or row.get("created_at"))
    updated = parse_time(row.get("updated_at") or row.get("created_at"))
    status = str(row.get("status") or "")
    end = now if status in ACTIVE else updated
    minutes = max((end - started).total_seconds() / 60.0, 0.0)
    head = row.get("head_commit") if isinstance(row.get("head_commit"), dict) else {}
    pulls = tuple(
        _int(item.get("number"))
        for item in row.get("pull_requests") or []
        if isinstance(item, dict) and item.get("number") is not None
    )
    return Run(
        id=_int(row.get("id")),
        repo=repo,
        workflow=str(row.get("name") or ""),
        workflow_id=_int(row.get("workflow_id")),
        path=str(row.get("path") or ""),
        event=str(row.get("event") or ""),
        status=status,
        conclusion=str(row.get("conclusion") or ""),
        head_branch=str(row.get("head_branch") or ""),
        head_sha=str(row.get("head_sha") or ""),
        run_attempt=max(_int(row.get("run_attempt"), 1), 1),
        created_at=created,
        started_at=started,
        updated_at=updated,
        message=str((head or {}).get("message") or row.get("display_title") or ""),
        pr_numbers=pulls,
        minutes=round(minutes, 2),
    )


def parse_runs(rows: list[dict], repo: str, *, now: datetime | None = None) -> list[Run]:
    return [parse_run(row, repo, now=now) for row in rows if isinstance(row, dict) and row.get("id")]
