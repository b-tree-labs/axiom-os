# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""GitLab diff extractor — pure Python, no LLM required.

Compares two gitlab_export JSON files (current vs. previous) and produces
Signals for: new issues, closed issues, commits by person, stale repos,
new usernames, and merge request activity.

Usage:
    extractor = GitLabDiffExtractor()
    extraction = extractor.extract(current_json, previous=previous_json)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from ..models import Extraction, Signal
from ..registry import SourceType, register_source
from .base import BaseExtractor


@register_source(
    name="gitlab",
    description="GitLab repository activity and issues",
    source_type=SourceType.PULL,
    requires_auth=True,
    auth_env_vars=["GITLAB_TOKEN"],
    file_patterns=["gitlab_export*.json"],
    default_poll_interval=1800,
    icon="🦨",
    category="code",
)
class GitLabDiffExtractor(BaseExtractor):
    """Diffs two GitLab export JSONs to produce structured signals."""

    @property
    def name(self) -> str:
        return "gitlab_diff"

    def can_handle(self, path: Path) -> bool:
        return path.exists() and path.suffix == ".json" and "gitlab_export" in path.name

    def extract(self, source: Path, **kwargs) -> Extraction:
        """Extract signals by diffing current vs. previous export.

        Args:
            source: Path to the current (newer) export JSON.
            previous: Path to the previous (older) export JSON (keyword arg).
                      If not provided, generates signals from current only.
        """
        previous_path: Path | None = kwargs.get("previous")

        try:
            current = self._load_export(source)
        except Exception as e:
            return Extraction(
                extractor=self.name,
                source_file=str(source),
                errors=[f"Failed to load current export: {e}"],
            )

        if previous_path:
            try:
                previous = self._load_export(previous_path)
            except Exception as e:
                return Extraction(
                    extractor=self.name,
                    source_file=str(source),
                    errors=[f"Failed to load previous export: {e}"],
                )
        else:
            previous = None

        now = datetime.now(UTC).isoformat()
        signals: list[Signal] = []
        errors: list[str] = []

        try:
            if previous:
                signals.extend(self._diff_exports(current, previous, now))
            else:
                signals.extend(self._summarize_single(current, now))
        except Exception as e:
            errors.append(f"Diff computation error: {e}")

        return Extraction(
            extractor=self.name,
            source_file=str(source),
            signals=signals,
            errors=errors,
        )

    @staticmethod
    def _load_export(path: Path) -> dict:
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def _diff_exports(
        self, current: dict, previous: dict, timestamp: str
    ) -> list[Signal]:
        """Compare two exports and produce diff signals."""
        signals = []

        curr_projects = {
            p["info"]["path_with_namespace"]: p for p in current.get("projects", [])
        }
        prev_projects = {
            p["info"]["path_with_namespace"]: p for p in previous.get("projects", [])
        }

        # New projects
        for path in set(curr_projects) - set(prev_projects):
            proj = curr_projects[path]
            signals.append(Signal(
                source=self.name,
                timestamp=timestamp,
                raw_text=f"New project discovered: {path}",
                signal_type="progress",
                detail=f"New GitLab project: {proj['info']['name']} ({path})",
                confidence=1.0,
                metadata={"project": path, "event": "new_project"},
            ))

        # Per-project diffs
        for path, curr_proj in curr_projects.items():
            prev_proj = prev_projects.get(path)
            if not prev_proj:
                continue

            curr_activity = curr_proj.get("activity", {})
            prev_activity = prev_proj.get("activity", {})

            if curr_activity.get("skipped") or prev_activity.get("skipped"):
                continue

            # New commits
            curr_shas = {c["sha"] for c in curr_activity.get("commits", [])}
            prev_shas = {c["sha"] for c in prev_activity.get("commits", [])}
            new_shas = curr_shas - prev_shas

            if new_shas:
                new_commits = [
                    c for c in curr_activity.get("commits", []) if c["sha"] in new_shas
                ]
                # Group by author
                by_author: dict[str, list] = {}
                for c in new_commits:
                    author = c.get("author_name", "Unknown")
                    by_author.setdefault(author, []).append(c)

                for author, commits in by_author.items():
                    # Use full message when available, fall back to title
                    messages = [
                        c.get("message") or c["title"] for c in commits
                    ]
                    first_title = commits[0]["title"]
                    signals.append(Signal(
                        source=self.name,
                        timestamp=timestamp,
                        raw_text="\n\n".join(messages),
                        people=[author],
                        initiatives=[self._guess_initiative(path)],
                        signal_type="progress",
                        detail=f"{author}: {len(commits)} new commit(s) in {path.split('/')[-1]} — {first_title}",
                        confidence=1.0,
                        metadata={
                            "project": path,
                            "event": "new_commits",
                            "commit_count": len(commits),
                            "shas": [c["sha"] for c in commits],
                        },
                    ))

            # New issues opened
            curr_open_iids = {i["iid"] for i in curr_activity.get("open_issues", [])}
            prev_open_iids = {i["iid"] for i in prev_activity.get("open_issues", [])}
            new_issue_iids = curr_open_iids - prev_open_iids

            for issue in curr_activity.get("open_issues", []):
                if issue["iid"] in new_issue_iids:
                    signals.append(Signal(
                        source=self.name,
                        timestamp=issue.get("created_at", timestamp),
                        raw_text=f"#{issue['iid']}: {issue['title']}",
                        people=issue.get("assignees", []),
                        initiatives=[self._guess_initiative(path)],
                        signal_type="action_item",
                        detail=f"New issue in {path.split('/')[-1]}: #{issue['iid']} {issue['title']}",
                        confidence=1.0,
                        metadata={
                            "project": path,
                            "event": "issue_opened",
                            "iid": issue["iid"],
                            "labels": issue.get("labels", []),
                        },
                    ))

            # Issues closed since last export
            curr_closed_iids = {
                i["iid"] for i in curr_activity.get("recently_closed_issues", [])
            }
            prev_closed_iids = {
                i["iid"] for i in prev_activity.get("recently_closed_issues", [])
            }
            newly_closed = curr_closed_iids - prev_closed_iids

            for issue in curr_activity.get("recently_closed_issues", []):
                if issue["iid"] in newly_closed:
                    signals.append(Signal(
                        source=self.name,
                        timestamp=issue.get("closed_at", timestamp),
                        raw_text=f"Closed #{issue['iid']}: {issue['title']}",
                        people=issue.get("assignees", []),
                        initiatives=[self._guess_initiative(path)],
                        signal_type="progress",
                        detail=f"Issue closed in {path.split('/')[-1]}: #{issue['iid']} {issue['title']}",
                        confidence=1.0,
                        metadata={
                            "project": path,
                            "event": "issue_closed",
                            "iid": issue["iid"],
                        },
                    ))

            # Stale repo detection
            curr_commits = curr_activity.get("commits", [])
            if not curr_commits and prev_activity.get("commits"):
                signals.append(Signal(
                    source=self.name,
                    timestamp=timestamp,
                    raw_text=f"No recent commits in {path}",
                    initiatives=[self._guess_initiative(path)],
                    signal_type="status_change",
                    detail=f"Repository gone stale: {path} (had commits before, none now)",
                    confidence=1.0,
                    metadata={"project": path, "event": "repo_stale"},
                ))

            # New issue comments
            curr_note_ids = {
                c["note_id"]
                for c in curr_activity.get("issue_comments", [])
            }
            prev_note_ids = {
                c["note_id"]
                for c in prev_activity.get("issue_comments", [])
            }
            new_note_ids = curr_note_ids - prev_note_ids

            if new_note_ids:
                new_comments = [
                    c for c in curr_activity.get("issue_comments", [])
                    if c["note_id"] in new_note_ids
                ]
                # Group by issue for readability
                by_issue: dict[int, list] = {}
                for c in new_comments:
                    by_issue.setdefault(c["issue_iid"], []).append(c)

                for iid, comments in by_issue.items():
                    issue_title = comments[0].get("issue_title", f"#{iid}")
                    authors = sorted({c["author"] for c in comments})
                    bodies = [
                        f"{c['author']}: {c['body']}" for c in comments
                    ]
                    signals.append(Signal(
                        source=self.name,
                        timestamp=comments[0].get("created_at", timestamp),
                        raw_text="\n\n".join(bodies),
                        people=authors,
                        initiatives=[self._guess_initiative(path)],
                        signal_type="progress",
                        detail=(
                            f"{len(comments)} new comment(s) on "
                            f"{path.split('/')[-1]}#{iid}: {issue_title}"
                        ),
                        confidence=1.0,
                        metadata={
                            "project": path,
                            "event": "issue_comments",
                            "iid": iid,
                            "note_ids": [c["note_id"] for c in comments],
                            "comment_count": len(comments),
                        },
                    ))

            # New contributors
            curr_authors = set(curr_activity.get("contributor_summary", {}).keys())
            prev_authors = set(prev_activity.get("contributor_summary", {}).keys())
            new_authors = curr_authors - prev_authors

            for author in new_authors:
                signals.append(Signal(
                    source=self.name,
                    timestamp=timestamp,
                    raw_text=f"New contributor: {author} in {path}",
                    people=[author],
                    initiatives=[self._guess_initiative(path)],
                    signal_type="status_change",
                    detail=f"New contributor in {path.split('/')[-1]}: {author}",
                    confidence=1.0,
                    metadata={"project": path, "event": "new_contributor"},
                ))

        return signals

    def _summarize_single(self, export: dict, timestamp: str) -> list[Signal]:
        """Generate summary signals from a single export (no diff)."""
        signals = []

        summary = export.get("summary", {})

        # Commits by author
        for author, count in summary.get("total_commits_by_author", {}).items():
            signals.append(Signal(
                source=self.name,
                timestamp=timestamp,
                raw_text=f"{author}: {count} commits",
                people=[author],
                signal_type="progress",
                detail=f"{author}: {count} commits in the export period",
                confidence=1.0,
                metadata={"event": "commit_summary", "count": count},
            ))

        # Stale repos
        for repo in summary.get("stale_repos", []):
            signals.append(Signal(
                source=self.name,
                timestamp=timestamp,
                raw_text=f"Stale repo: {repo}",
                initiatives=[self._guess_initiative(repo)],
                signal_type="status_change",
                detail=f"Repository stale (no commits in 30 days): {repo}",
                confidence=1.0,
                metadata={"project": repo, "event": "repo_stale"},
            ))

        # New projects
        for proj in summary.get("newly_discovered_projects", []):
            signals.append(Signal(
                source=self.name,
                timestamp=timestamp,
                raw_text=f"New project: {proj['path']}",
                initiatives=[self._guess_initiative(proj["path"])],
                signal_type="progress",
                detail=f"Newly discovered project: {proj['name']} ({proj['path']})",
                confidence=1.0,
                metadata={"project": proj["path"], "event": "new_project"},
            ))

        # Issue comment activity (from per-project data)
        total_comments = summary.get("total_issue_comments", 0)
        if total_comments:
            signals.append(Signal(
                source=self.name,
                timestamp=timestamp,
                raw_text=f"{total_comments} issue comments in the export period",
                signal_type="progress",
                detail=f"{total_comments} issue comment(s) across all projects",
                confidence=1.0,
                metadata={"event": "comment_summary", "count": total_comments},
            ))

        return signals

    @staticmethod
    def _guess_initiative(project_path: str) -> str:
        """Guess initiative name from GitLab project path.

        Maps repo paths to initiative names for correlation.
        Mappings are loaded from config/gitlab_initiatives.json if available,
        falling back to extracting the repo name from the path.
        """
        path_lower = project_path.lower()

        # Try to load custom mappings from config
        mappings = GitLabDiffExtractor._load_initiative_mappings()

        for fragment, initiative in mappings.items():
            if fragment in path_lower:
                return initiative

        return project_path.split("/")[-1] if "/" in project_path else project_path

    @staticmethod
    def _load_initiative_mappings() -> dict[str, str]:
        """Load repo path → initiative name mappings from config.

        Looks for runtime/config/gitlab_initiatives.json.
        Returns empty dict if not found (falls back to repo name extraction).
        """
        import json
        from pathlib import Path

        config_path = Path("runtime/config/gitlab_initiatives.json")
        if config_path.exists():
            try:
                return json.loads(config_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {}
