# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Base ABC and shared data models for repo sensing providers.

Each provider implements RepoSourceProvider to fetch activity from a
single code-hosting platform (GitLab, GitHub, etc.). Data is normalised
into source-agnostic dataclasses so the orchestrator and downstream
extractors never depend on a specific API.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from axiom.infra.time_utils import parse_iso

# ---------------------------------------------------------------------------
# Source-agnostic data models
# ---------------------------------------------------------------------------


@dataclass
class RepoInfo:
    """Minimal metadata for a single repository."""

    id: str  # unique within the source
    name: str
    full_path: str  # e.g. "example-org/example-consumer"
    url: str
    default_branch: str
    last_activity_at: str | None
    source: str  # "gitlab" or "github"


@dataclass
class RepoActivity:
    """Activity snapshot for a single repository over a time window."""

    commits: list[dict] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    merge_requests: list[dict] = field(default_factory=list)  # PRs for GitHub
    branches: list[dict] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    milestones: list[dict] = field(default_factory=list)
    contributor_summary: dict[str, int] = field(default_factory=dict)
    issue_comments: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Provider ABC
# ---------------------------------------------------------------------------


class RepoSourceProvider(ABC):
    """Fetches repository activity from a single source (GitLab, GitHub, etc.)."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this provider (e.g. 'gitlab', 'github')."""
        ...

    @abstractmethod
    def authenticate(self) -> bool:
        """Validate token / credentials.  Return True on success."""
        ...

    @abstractmethod
    def discover_repos(self) -> list[RepoInfo]:
        """Org / group-level discovery — return all visible repos."""
        ...

    @abstractmethod
    def get_activity(self, repo: RepoInfo, days: int) -> RepoActivity:
        """Fetch activity for a single repo within *days* window."""
        ...


# ---------------------------------------------------------------------------
# Shared utilities (extracted from gitlab_tracker_export.py)
# ---------------------------------------------------------------------------

MAX_DESCRIPTION_LENGTH = 200
MAX_COMMIT_MESSAGE_LENGTH = 200


def truncate(text: str | None, max_length: int) -> str | None:
    """Truncate *text* to *max_length*, adding ellipsis if needed."""
    if text is None:
        return None
    text = text.strip()
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."


def parse_datetime(dt_str: str | None) -> datetime | None:
    """Parse an ISO-8601 datetime string to a *datetime* object."""
    if not dt_str:
        return None
    try:
        return parse_iso(dt_str)
    except (ValueError, TypeError):
        return None


def is_within_days(dt_str: str | None, days: int) -> bool:
    """Return True if *dt_str* falls within the last *days* days."""
    dt = parse_datetime(dt_str)
    if not dt:
        return False
    cutoff = datetime.now(UTC) - timedelta(days=days)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt >= cutoff


def retry_on_rate_limit(func, max_retries: int = 3, base_delay: float = 5.0):
    """Call *func* with exponential back-off on HTTP 429 responses.

    Works with both python-gitlab (GitlabHttpError) and PyGithub
    (RateLimitExceededException) by catching generic exceptions and
    checking for rate-limit indicators.
    """
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as exc:
            # python-gitlab: GitlabHttpError with response_code 429
            code = getattr(exc, "response_code", None) or getattr(exc, "status", None)
            is_rate_limit = code == 429
            # PyGithub: RateLimitExceededException
            if not is_rate_limit and type(exc).__name__ == "RateLimitExceededException":
                is_rate_limit = True
            if is_rate_limit:
                delay = base_delay * (2 ** attempt)
                print(f"  Rate limited, waiting {delay:.0f}s...")
                time.sleep(delay)
            else:
                raise
    return func()  # final attempt
