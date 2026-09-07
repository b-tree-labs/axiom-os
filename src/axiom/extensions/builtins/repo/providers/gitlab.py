# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""GitLab repo sensing provider.

Extracted from tools/gitlab_tracker_export.py.  Implements
RepoSourceProvider to discover repos within a GitLab group and
fetch per-repo activity data.

Requires:
    pip install python-gitlab
    GITLAB_TOKEN env var with read_api + read_repository scopes
"""

from __future__ import annotations

import os
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from axiom.extensions.builtins.repo.base import (
    MAX_COMMIT_MESSAGE_LENGTH,
    MAX_DESCRIPTION_LENGTH,
    RepoActivity,
    RepoInfo,
    RepoSourceProvider,
    is_within_days,
    retry_on_rate_limit,
    truncate,
)


class GitLabProvider(RepoSourceProvider):
    """Fetch repo activity from a GitLab instance."""

    def __init__(
        self,
        url: str = "https://gitlab.example.com",
        group: str = "my-org",
        token_env: str = "GITLAB_TOKEN",
        max_commits: int = 50,
    ):
        self.url = url
        self.group = group
        self.token_env = token_env
        self.max_commits = max_commits
        self._gl = None

    # -- ABC implementation -------------------------------------------------

    @property
    def name(self) -> str:
        return "gitlab"

    def authenticate(self) -> bool:
        """Validate the GitLab token and cache the client."""
        token = os.environ.get(self.token_env)
        if not token:
            print(f"  GitLab: {self.token_env} not set")
            return False
        try:
            import gitlab
        except ImportError:
            print("  GitLab: python-gitlab not installed (pip install python-gitlab)")
            return False

        try:
            gl = gitlab.Gitlab(self.url, private_token=token)
            gl.auth()
            self._gl = gl
            user = gl.user
            print(f"  GitLab: authenticated as {user.username}" if user else "  GitLab: authenticated")
            return True
        except Exception as exc:
            hint = ""
            exc_str = str(exc)
            if "401" in exc_str:
                hint = " — check that your token is valid and not expired"
            elif "403" in exc_str:
                hint = " — token may lack required scopes (needs: read_api, read_repository)"
            print(f"  GitLab: auth failed — {exc}{hint}")
            return False

    def discover_repos(self) -> list[RepoInfo]:
        """Recursively discover all projects in the configured group."""
        gl = self._ensure_client()
        from gitlab.exceptions import GitlabGetError

        try:
            group = gl.groups.get(self.group)
        except GitlabGetError as exc:
            print(f"  GitLab: could not access group '{self.group}': {exc}")
            return []

        projects: list[dict] = []
        self._collect_projects_recursive(gl, group, projects)

        return [
            RepoInfo(
                id=str(p["id"]),
                name=p["name"],
                full_path=p["path_with_namespace"],
                url=p["web_url"],
                default_branch=p.get("default_branch", "main"),
                last_activity_at=p.get("last_activity_at"),
                source="gitlab",
            )
            for p in projects
        ]

    def get_activity(self, repo: RepoInfo, days: int) -> RepoActivity:
        """Fetch commits, issues, MRs, branches, labels, milestones for *repo*."""
        gl = self._ensure_client()
        from gitlab.exceptions import GitlabHttpError

        try:
            project = gl.projects.get(int(repo.id))
        except GitlabHttpError as exc:
            if getattr(exc, "response_code", None) == 403:
                return RepoActivity()
            raise

        cutoff = datetime.now(UTC) - timedelta(days=days)

        activity = RepoActivity()
        activity.commits = self._get_commits(project, cutoff)
        activity.contributor_summary = self._compute_contributor_summary(activity.commits)
        activity.issues = self._get_open_issues(project)
        recently_closed = self._get_closed_issues(project, cutoff, days)
        activity.issues.extend(recently_closed)
        activity.issue_comments = self._get_issue_comments(
            project, activity.issues, cutoff, days,
        )
        activity.merge_requests = self._get_open_mrs(project)
        activity.merge_requests.extend(self._get_merged_mrs(project, cutoff, days))
        activity.milestones = self._get_milestones(project)
        activity.labels = self._get_labels(project)
        activity.branches = self._get_active_branches(project, days)
        return activity

    # -- Internal helpers ---------------------------------------------------

    def _ensure_client(self):
        if self._gl is None:
            raise RuntimeError("Call authenticate() before using the GitLab provider")
        return self._gl

    # -- Project discovery --------------------------------------------------

    def _collect_projects_recursive(
        self, gl, group, projects: list[dict], depth: int = 0,
    ) -> None:
        from gitlab.exceptions import GitlabHttpError

        indent = "  " * (depth + 1)

        try:
            for project in retry_on_rate_limit(
                lambda: list(group.projects.list(iterator=True, include_subgroups=False))
            ):
                try:
                    full_project = gl.projects.get(project.id)
                    projects.append({
                        "id": project.id,
                        "name": project.name,
                        "path": project.path,
                        "path_with_namespace": project.path_with_namespace,
                        "description": truncate(getattr(project, "description", None), MAX_DESCRIPTION_LENGTH),
                        "default_branch": getattr(full_project, "default_branch", "main"),
                        "last_activity_at": getattr(project, "last_activity_at", None),
                        "web_url": project.web_url,
                    })
                    print(f"{indent}+ {project.path_with_namespace}")
                except GitlabHttpError as exc:
                    if getattr(exc, "response_code", None) == 403:
                        print(f"{indent}  (skipped — 403) {project.path_with_namespace}")
                    else:
                        raise
        except GitlabHttpError as exc:
            if getattr(exc, "response_code", None) == 403:
                print(f"{indent}  (cannot list projects — 403)")
            else:
                raise

        try:
            subgroups = retry_on_rate_limit(lambda: list(group.subgroups.list(iterator=True)))
            for subgroup in subgroups:
                print(f"{indent}  {subgroup.full_path}/")
                full_subgroup = gl.groups.get(subgroup.id)
                self._collect_projects_recursive(gl, full_subgroup, projects, depth + 1)
        except GitlabHttpError as exc:
            if getattr(exc, "response_code", None) == 403:
                print(f"{indent}  (cannot list subgroups — 403)")
            else:
                raise

    # -- Per-repo activity fetchers -----------------------------------------

    def _get_commits(self, project, cutoff: datetime) -> list[dict]:
        from gitlab.exceptions import GitlabHttpError

        commits = []
        try:
            since_str = cutoff.isoformat()
            for commit in retry_on_rate_limit(
                lambda: list(
                    project.commits.list(
                        since=since_str,
                        per_page=self.max_commits,
                        get_all=False,
                    )
                )
            ):
                commits.append({
                    "sha": commit.short_id,
                    "author_name": commit.author_name,
                    "author_email": commit.author_email,
                    "created_at": commit.created_at,
                    "title": truncate(commit.title, MAX_COMMIT_MESSAGE_LENGTH),
                    "message": truncate(
                        getattr(commit, "message", None) or commit.title, 500,
                    ),
                })
        except GitlabHttpError as exc:
            if getattr(exc, "response_code", None) != 403:
                raise
        return commits

    @staticmethod
    def _compute_contributor_summary(commits: list[dict]) -> dict[str, int]:
        summary: dict[str, int] = defaultdict(int)
        for commit in commits:
            author = commit.get("author_name", "Unknown")
            summary[author] += 1
        return dict(sorted(summary.items(), key=lambda x: -x[1]))

    def _get_open_issues(self, project) -> list[dict]:
        from gitlab.exceptions import GitlabHttpError

        issues = []
        try:
            for issue in retry_on_rate_limit(
                lambda: list(project.issues.list(state="opened", iterator=True))
            ):
                issues.append(self._format_issue(issue))
        except GitlabHttpError as exc:
            if getattr(exc, "response_code", None) != 403:
                raise
        return issues

    def _get_closed_issues(self, project, cutoff: datetime, days: int) -> list[dict]:
        from gitlab.exceptions import GitlabHttpError

        issues = []
        try:
            since_str = cutoff.isoformat()
            for issue in retry_on_rate_limit(
                lambda: list(
                    project.issues.list(state="closed", updated_after=since_str, iterator=True)
                )
            ):
                if is_within_days(getattr(issue, "closed_at", None), days):
                    data = self._format_issue(issue)
                    data["closed_at"] = getattr(issue, "closed_at", None)
                    issues.append(data)
        except GitlabHttpError as exc:
            if getattr(exc, "response_code", None) != 403:
                raise
        return issues

    def _get_issue_comments(
        self, project, issues: list[dict], cutoff: datetime, days: int,
        max_notes_per_issue: int = 20,
    ) -> list[dict]:
        from gitlab.exceptions import GitlabHttpError

        comments: list[dict] = []
        for issue_data in issues:
            iid = issue_data["iid"]
            try:
                issue_obj = project.issues.get(iid)
                for note in retry_on_rate_limit(
                    lambda _obj=issue_obj: list(
                        _obj.notes.list(
                            per_page=max_notes_per_issue,
                            get_all=False,
                            order_by="created_at",
                            sort="desc",
                        )
                    )
                ):
                    if getattr(note, "system", False):
                        continue
                    if not is_within_days(getattr(note, "created_at", None), days):
                        continue
                    author_data = getattr(note, "author", {}) or {}
                    comments.append({
                        "issue_iid": iid,
                        "issue_title": issue_data.get("title", ""),
                        "note_id": note.id,
                        "author": author_data.get("username", author_data.get("name", "")),
                        "body": truncate(note.body, 500),
                        "created_at": note.created_at,
                    })
            except GitlabHttpError as exc:
                if getattr(exc, "response_code", None) != 403:
                    raise
        return comments

    @staticmethod
    def _format_issue(issue) -> dict:
        assignees: list[str] = []
        if hasattr(issue, "assignees") and issue.assignees:
            assignees = [a.get("username", a.get("name", "")) for a in issue.assignees]
        elif hasattr(issue, "assignee") and issue.assignee:
            assignees = [issue.assignee.get("username", issue.assignee.get("name", ""))]

        return {
            "iid": issue.iid,
            "title": issue.title,
            "labels": issue.labels if hasattr(issue, "labels") else [],
            "assignees": assignees,
            "author": (
                issue.author.get("username", "")
                if hasattr(issue, "author") and issue.author
                else ""
            ),
            "created_at": issue.created_at,
            "updated_at": issue.updated_at,
            "milestone": (
                issue.milestone.get("title", "")
                if hasattr(issue, "milestone") and issue.milestone
                else None
            ),
            "description": truncate(getattr(issue, "description", None), MAX_DESCRIPTION_LENGTH),
        }

    def _get_open_mrs(self, project) -> list[dict]:
        from gitlab.exceptions import GitlabHttpError

        mrs = []
        try:
            for mr in retry_on_rate_limit(
                lambda: list(project.mergerequests.list(state="opened", iterator=True))
            ):
                mrs.append(self._format_mr(mr))
        except GitlabHttpError as exc:
            if getattr(exc, "response_code", None) != 403:
                raise
        return mrs

    def _get_merged_mrs(self, project, cutoff: datetime, days: int) -> list[dict]:
        from gitlab.exceptions import GitlabHttpError

        mrs = []
        try:
            since_str = cutoff.isoformat()
            for mr in retry_on_rate_limit(
                lambda: list(
                    project.mergerequests.list(state="merged", updated_after=since_str, iterator=True)
                )
            ):
                if is_within_days(getattr(mr, "merged_at", None), days):
                    mr_data = self._format_mr(mr)
                    mr_data["merged_at"] = getattr(mr, "merged_at", None)
                    mrs.append(mr_data)
        except GitlabHttpError as exc:
            if getattr(exc, "response_code", None) != 403:
                raise
        return mrs

    @staticmethod
    def _format_mr(mr) -> dict:
        return {
            "iid": mr.iid,
            "title": mr.title,
            "author": (
                mr.author.get("username", "")
                if hasattr(mr, "author") and mr.author
                else ""
            ),
            "assignee": (
                mr.assignee.get("username", "")
                if hasattr(mr, "assignee") and mr.assignee
                else None
            ),
            "source_branch": mr.source_branch,
            "created_at": mr.created_at,
        }

    def _get_milestones(self, project) -> list[dict]:
        from gitlab.exceptions import GitlabHttpError

        milestones = []
        try:
            for ms in retry_on_rate_limit(lambda: list(project.milestones.list(iterator=True))):
                milestones.append({
                    "title": ms.title,
                    "state": ms.state,
                    "due_date": getattr(ms, "due_date", None),
                    "description": truncate(getattr(ms, "description", None), MAX_DESCRIPTION_LENGTH),
                })
        except GitlabHttpError as exc:
            if getattr(exc, "response_code", None) != 403:
                raise
        return milestones

    def _get_labels(self, project) -> list[str]:
        from gitlab.exceptions import GitlabHttpError

        labels = []
        try:
            for label in retry_on_rate_limit(lambda: list(project.labels.list(iterator=True))):
                labels.append(label.name)
        except GitlabHttpError as exc:
            if getattr(exc, "response_code", None) != 403:
                raise
        return sorted(labels)

    def _get_active_branches(self, project, days: int) -> list[dict]:
        from gitlab.exceptions import GitlabHttpError

        branches = []
        try:
            for branch in retry_on_rate_limit(lambda: list(project.branches.list(iterator=True))):
                commit = branch.commit if hasattr(branch, "commit") else {}
                commit_date = commit.get("committed_date") if commit else None

                if is_within_days(commit_date, days):
                    branches.append({
                        "name": branch.name,
                        "merged": getattr(branch, "merged", False),
                        "last_commit_date": commit_date,
                        "last_commit_author": commit.get("author_name") if commit else None,
                    })
        except GitlabHttpError as exc:
            if getattr(exc, "response_code", None) != 403:
                raise
        return branches
