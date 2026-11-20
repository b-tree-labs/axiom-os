# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Pull-request shapes the lander shares. No forge-specific fields."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def parse_time(value: str | datetime | None) -> datetime:
    """ISO-8601 to an aware datetime. Missing values become now."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return datetime.now(UTC)
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


@dataclass(frozen=True)
class FailingCheck:
    """One red check and a URL for its log excerpt."""

    name: str
    log_url: str = ""


@dataclass(frozen=True)
class OpenPull:
    """One open pull request, already normalised."""

    number: int
    title: str
    head_sha: str
    head_ref: str
    base_ref: str = "main"
    author: str = ""
    labels: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    draft: bool = False
    mergeable: bool | None = None
    checks: str = "unknown"
    ci_attempts: int = 0
    ticket_ids: tuple[str, ...] = ()
    additions: int = 0
    deletions: int = 0
    url: str = ""
    body: str = ""
    failure_log: str = ""
    author_model: str = ""
    #: Set from a test selector. Empty means the selector could not name a
    #: smaller set, so affected-scope grouping does not apply.
    scope: str = ""
    failing_checks: tuple[FailingCheck, ...] = ()
    #: Humans with an open review request. Teams and bots are not included.
    pending_human_reviewers: tuple[str, ...] = ()
    #: Humans whose latest review asks for changes.
    changes_requested_by: tuple[str, ...] = ()
    #: The forge's merge state (``BEHIND``, ``CLEAN``, ``BLOCKED``...). Empty when unknown.
    merge_state: str = ""

    def sorted_files(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.files)))


def failing_checks_from(raw: object) -> tuple[FailingCheck, ...]:
    """Accept mappings or ``(name, log_url)`` pairs. Unknown shapes are skipped."""
    if not isinstance(raw, (list, tuple)):
        return ()
    checks: list[FailingCheck] = []
    for item in raw:
        if isinstance(item, FailingCheck):
            checks.append(item)
        elif isinstance(item, dict):
            checks.append(FailingCheck(str(item.get("name") or ""), str(item.get("log_url") or "")))
        elif isinstance(item, (list, tuple)) and item:
            url = str(item[1]) if len(item) > 1 else ""
            checks.append(FailingCheck(str(item[0]), url))
    return tuple(check for check in checks if check.name)


def pull_from_mapping(row: dict[str, Any]) -> OpenPull:
    """Build an OpenPull from a plain mapping (tests and ``--pulls``)."""
    labels = row.get("labels") or ()
    files = row.get("files") or ()
    tickets = row.get("ticket_ids") or ()
    return OpenPull(
        number=int(row["number"]),
        title=str(row.get("title") or ""),
        head_sha=str(row.get("head_sha") or row.get("headRefOid") or ""),
        head_ref=str(row.get("head_ref") or row.get("headRefName") or ""),
        base_ref=str(row.get("base_ref") or row.get("baseRefName") or "main"),
        author=str(row.get("author") or ""),
        labels=tuple(str(item) for item in labels),
        files=tuple(sorted(set(str(item) for item in files))),
        updated_at=parse_time(row.get("updated_at") or row.get("updatedAt")),
        created_at=parse_time(row.get("created_at") or row.get("createdAt")),
        draft=bool(row.get("draft") or row.get("isDraft") or False),
        mergeable=row.get("mergeable"),
        checks=str(row.get("checks") or "unknown"),
        ci_attempts=int(row.get("ci_attempts") or 0),
        ticket_ids=tuple(str(item) for item in tickets),
        additions=int(row.get("additions") or 0),
        deletions=int(row.get("deletions") or 0),
        url=str(row.get("url") or ""),
        body=str(row.get("body") or ""),
        failure_log=str(row.get("failure_log") or ""),
        author_model=str(row.get("author_model") or ""),
        failing_checks=failing_checks_from(row.get("failing_checks")),
        pending_human_reviewers=tuple(
            str(item) for item in (row.get("pending_human_reviewers") or ())
        ),
        changes_requested_by=tuple(str(item) for item in (row.get("changes_requested_by") or ())),
        merge_state=str(row.get("merge_state") or row.get("mergeStateStatus") or "").upper(),
    )
