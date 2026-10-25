# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""GitHub repo sensing provider.

Implements RepoSourceProvider for org-level repo discovery and per-repo
activity fetching via PyGithub.

Requires:
    pip install PyGithub
    GITHUB_TOKEN env var with repo read scopes
"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from axiom.extensions.builtins.repo.base import (
    RepoActivity,
    RepoInfo,
    RepoSourceProvider,
    truncate,
)


class GitHubProvider(RepoSourceProvider):
    """Fetch repo activity from a GitHub organisation."""

    def __init__(
        self,
        org: str = "UT-Computational-NE",
        token_env: str = "GITHUB_TOKEN",
    ):
        self.org = org
        self.token_env = token_env
        self._client = None

    # -- ABC implementation -------------------------------------------------

    @property
    def name(self) -> str:
        return "github"

    def authenticate(self) -> bool:
        token = os.environ.get(self.token_env)
        if not token:
            print(f"  GitHub: {self.token_env} not set")
            return False
        try:
            from github import Auth, Github  # type: ignore[import-untyped]
        except ImportError:
            print("  GitHub: PyGithub not installed (pip install PyGithub)")
            return False

        try:
            client = Github(auth=Auth.Token(token))
            user = client.get_user()
            # Force a network call to validate the token
            _ = user.login
            self._client = client
            print(f"  GitHub: authenticated as {user.login}")
            return True
        except Exception as exc:
            hint = ""
            exc_str = str(exc)
            if "401" in exc_str or "Bad credentials" in exc_str:
                hint = " — check that your token is valid and not expired"
            elif "403" in exc_str:
                hint = " — token may lack required permissions (needs: Contents, Issues, Pull requests, Metadata)"
            elif "404" in exc_str:
                hint = " — org not found or token has no access to it"
            print(f"  GitHub: auth failed — {exc}{hint}")
            return False

    def discover_repos(self) -> list[RepoInfo]:
        """List all repos in the configured organisation."""
        client = self._ensure_client()
        repos: list[RepoInfo] = []

        try:
            org = client.get_organization(self.org)
            for repo in org.get_repos(type="all"):
                repos.append(RepoInfo(
                    id=str(repo.id),
                    name=repo.name,
                    full_path=repo.full_name,
                    url=repo.html_url,
                    default_branch=repo.default_branch or "main",
                    last_activity_at=(
                        repo.pushed_at.isoformat() if repo.pushed_at else None
                    ),
                    source="github",
                ))
                print(f"    + {repo.full_name}")
        except Exception as exc:
            exc_str = str(exc)
            if "lifetime" in exc_str or "366 days" in exc_str:
                print("  GitHub: org requires token expiration ≤ 1 year — edit your token at github.com/settings/personal-access-tokens")
            elif "403" in exc_str:
                print(f"  GitHub: access denied for {self.org} — check token permissions and org membership")
            else:
                print(f"  GitHub: could not list repos for {self.org} — {exc}")

        return repos

    def get_activity(self, repo: RepoInfo, days: int) -> RepoActivity:
        """Fetch commits, PRs, issues for a single GitHub repo."""
        client = self._ensure_client()
        since = datetime.now(UTC) - timedelta(days=days)

        try:
            gh_repo = client.get_repo(repo.full_path)
        except Exception as exc:
            print(f"  GitHub: could not access {repo.full_path}: {exc}")
            return RepoActivity()

        activity = RepoActivity()

        # -- Commits --------------------------------------------------------
        try:
            for commit in gh_repo.get_commits(since=since):
                author = commit.commit.author
                activity.commits.append({
                    "sha": commit.sha[:8],
                    "author_name": author.name if author else "Unknown",
                    "author_email": author.email if author else "",
                    "created_at": author.date.isoformat() if author else "",
                    "title": truncate(
                        commit.commit.message.split("\n", 1)[0], 200,
                    ),
                    "message": truncate(commit.commit.message, 500),
                })
        except Exception as exc:
            print(f"    commits: {exc}")

        # -- Contributor summary -------------------------------------------
        summary: dict[str, int] = defaultdict(int)
        for c in activity.commits:
            summary[c.get("author_name", "Unknown")] += 1
        activity.contributor_summary = dict(sorted(summary.items(), key=lambda x: -x[1]))

        # -- Pull requests (merge_requests field) --------------------------
        try:
            for pr in gh_repo.get_pulls(state="all", sort="updated", direction="desc"):
                if pr.updated_at and pr.updated_at < since:
                    break
                activity.merge_requests.append({
                    "iid": pr.number,
                    "title": pr.title,
                    "author": pr.user.login if pr.user else "Unknown",
                    "assignee": (
                        pr.assignee.login if pr.assignee else None
                    ),
                    "source_branch": pr.head.ref if pr.head else "",
                    "created_at": pr.created_at.isoformat() if pr.created_at else "",
                    "merged_at": pr.merged_at.isoformat() if pr.merged_at else None,
                    "state": pr.state,
                    "url": pr.html_url,
                    "body": truncate(pr.body, 500),
                })
        except Exception as exc:
            print(f"    pull requests: {exc}")

        # -- Issues (excluding PRs) ----------------------------------------
        try:
            for issue in gh_repo.get_issues(state="all", sort="updated", direction="desc"):
                if issue.updated_at and issue.updated_at < since:
                    break
                if issue.pull_request:
                    continue
                assignees = [a.login for a in issue.assignees] if issue.assignees else []
                activity.issues.append({
                    "iid": issue.number,
                    "title": issue.title,
                    "labels": [lbl.name for lbl in issue.labels],
                    "assignees": assignees,
                    "author": issue.user.login if issue.user else "",
                    "created_at": issue.created_at.isoformat() if issue.created_at else "",
                    "updated_at": issue.updated_at.isoformat() if issue.updated_at else "",
                    "closed_at": issue.closed_at.isoformat() if issue.closed_at else None,
                    "milestone": (
                        issue.milestone.title if issue.milestone else None
                    ),
                    "description": truncate(issue.body, 200),
                })
        except Exception as exc:
            print(f"    issues: {exc}")

        # -- Labels --------------------------------------------------------
        try:
            activity.labels = sorted(lbl.name for lbl in gh_repo.get_labels())
        except Exception:
            pass

        # -- Milestones ----------------------------------------------------
        try:
            for ms in gh_repo.get_milestones(state="all"):
                activity.milestones.append({
                    "title": ms.title,
                    "state": ms.state,
                    "due_date": ms.due_on.isoformat() if ms.due_on else None,
                    "description": truncate(ms.description, 200),
                })
        except Exception:
            pass

        # -- Active branches -----------------------------------------------
        try:
            for branch in gh_repo.get_branches():
                commit = branch.commit
                if commit and commit.commit and commit.commit.author:
                    date_str = commit.commit.author.date.isoformat()
                    dt = commit.commit.author.date
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=UTC)
                    if dt >= since:
                        activity.branches.append({
                            "name": branch.name,
                            "merged": branch.name == gh_repo.default_branch,
                            "last_commit_date": date_str,
                            "last_commit_author": commit.commit.author.name,
                        })
        except Exception:
            pass

        return activity

    # -- Internal -----------------------------------------------------------

    def _ensure_client(self):
        if self._client is None:
            raise RuntimeError("Call authenticate() before using the GitHub provider")
        return self._client
