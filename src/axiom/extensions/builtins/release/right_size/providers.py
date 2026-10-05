# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Repo-provider abstraction for ``right_size_pr``.

Each provider (GitHub today; GitLab, Gitea, Bitbucket next) implements
the ``RepoProvider`` Protocol with two read-only operations:

- ``list_in_flight_prs(repo, author)`` — open PRs the proposed change
  might compose with.
- ``diff_for_pr(repo, pr_number)`` — file-level diff of an in-flight PR.

The factory ``detect_provider`` returns the right adapter given a remote
URL or an explicit config name. Adapters never share state; the protocol
is the only contract.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol


# ---------------------------------------------------------------------------
# Provider-agnostic data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InFlightPR:
    """One open PR descriptor — provider-agnostic."""

    number: int
    title: str
    head_branch: str
    base_branch: str
    author: str
    url: str
    is_draft: bool
    created_at: datetime
    updated_at: datetime
    provider: str


@dataclass(frozen=True)
class PRDiff:
    """File-level diff for one PR — what we need to compute overlap.

    ``additions`` / ``deletions`` are aggregate counts (cheap to fetch);
    ``files`` lists the touched paths.
    """

    pr_number: int
    files: tuple[str, ...]
    additions: int
    deletions: int
    provider: str


# ---------------------------------------------------------------------------
# Provider Protocol
# ---------------------------------------------------------------------------


class RepoProvider(Protocol):
    name: str

    def list_in_flight_prs(
        self, repo: str, author: str | None = None
    ) -> list[InFlightPR]: ...

    def diff_for_pr(self, repo: str, pr_number: int) -> PRDiff | None: ...


# ---------------------------------------------------------------------------
# GitHub adapter (via `gh` CLI)
# ---------------------------------------------------------------------------


@dataclass
class GitHubProvider:
    """GitHub adapter via the ``gh`` CLI.

    Tests inject a ``runner`` to stub the subprocess; production uses
    the default ``subprocess.run``.
    """

    name: str = "github"
    runner: Callable = field(default=subprocess.run)

    def list_in_flight_prs(
        self, repo: str, author: str | None = None
    ) -> list[InFlightPR]:
        args = [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--json",
            "number,title,headRefName,baseRefName,author,url,"
            "isDraft,createdAt,updatedAt",
            "--limit",
            "100",
        ]
        if author:
            args.extend(["--author", author])
        out = self._gh(args)
        if not out:
            return []
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return []
        return [
            InFlightPR(
                number=int(d["number"]),
                title=d.get("title", ""),
                head_branch=d.get("headRefName", ""),
                base_branch=d.get("baseRefName", ""),
                author=(d.get("author") or {}).get("login", ""),
                url=d.get("url", ""),
                is_draft=bool(d.get("isDraft", False)),
                created_at=_parse_iso(d.get("createdAt")),
                updated_at=_parse_iso(d.get("updatedAt")),
                provider=self.name,
            )
            for d in data
        ]

    def diff_for_pr(self, repo: str, pr_number: int) -> PRDiff | None:
        out = self._gh(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repo,
                "--json",
                "files,additions,deletions",
            ]
        )
        if not out:
            return None
        try:
            d = json.loads(out)
        except json.JSONDecodeError:
            return None
        return PRDiff(
            pr_number=pr_number,
            files=tuple(f["path"] for f in d.get("files", [])),
            additions=int(d.get("additions", 0)),
            deletions=int(d.get("deletions", 0)),
            provider=self.name,
        )

    def _gh(self, args: list[str]) -> str:
        try:
            r = self.runner(
                args, capture_output=True, text=True, check=False
            )
        except FileNotFoundError:
            return ""
        return r.stdout if r.returncode == 0 else ""


def _parse_iso(s: str | None) -> datetime:
    if not s:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# GitLab adapter — stub for the contract; full impl is a follow-up
# ---------------------------------------------------------------------------


@dataclass
class GitLabProvider:
    """GitLab adapter via the ``glab`` CLI. Stub today — same Protocol
    surface so consumers wire it identically when the implementation lands.
    """

    name: str = "gitlab"
    runner: Callable = field(default=subprocess.run)

    def list_in_flight_prs(
        self, repo: str, author: str | None = None
    ) -> list[InFlightPR]:
        return []  # full implementation = follow-up

    def diff_for_pr(self, repo: str, pr_number: int) -> PRDiff | None:
        return None  # full implementation = follow-up


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def detect_provider(
    *,
    remote_url: str | None = None,
    explicit: str | None = None,
    runner: Callable = subprocess.run,
) -> RepoProvider:
    """Return the right adapter for the given remote / config.

    Resolution order:
      1. ``explicit`` argument wins (``"github"`` / ``"gitlab"``).
      2. ``remote_url`` substring match (``github.com`` / ``gitlab.com``
         / ``gitlab.<self-hosted>``).
      3. Default to GitHub (most installs).
    """
    if explicit:
        key = explicit.lower()
    elif remote_url:
        key = "gitlab" if "gitlab" in remote_url.lower() else "github"
    else:
        key = "github"
    if key == "gitlab":
        return GitLabProvider(runner=runner)
    return GitHubProvider(runner=runner)


__all__ = [
    "GitHubProvider",
    "GitLabProvider",
    "InFlightPR",
    "PRDiff",
    "RepoProvider",
    "detect_provider",
]
