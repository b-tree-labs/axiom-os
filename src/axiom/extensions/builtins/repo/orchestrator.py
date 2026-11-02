# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Repo sensing orchestrator — runs all configured providers and writes unified JSON.

Can be executed directly:
    python -m tools.repo_sensing.orchestrator --output-dir tools/exports
    python -m tools.repo_sensing.orchestrator --dry-run

Or used as a library:
    from axiom.extensions.builtins.repo.orchestrator import RepoExportOrchestrator
    orch = RepoExportOrchestrator()
    orch.run(days=90, output_dir=Path("exports"))
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from axiom.extensions.builtins.repo.base import RepoSourceProvider, is_within_days
from axiom.extensions.builtins.repo.config import SourceConfig, load_config

# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------


def _create_provider(source: SourceConfig) -> RepoSourceProvider:
    """Instantiate the appropriate provider for *source*."""
    if source.provider == "gitlab":
        from axiom.extensions.builtins.repo.providers.gitlab import GitLabProvider
        return GitLabProvider(
            url=source.url,
            group=source.group_or_org,
            token_env=source.token_env,
        )
    elif source.provider == "github":
        from axiom.extensions.builtins.repo.providers.github import GitHubProvider
        return GitHubProvider(
            org=source.group_or_org,
            token_env=source.token_env,
        )
    else:
        raise ValueError(f"Unknown repo source provider: {source.provider}")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class RepoExportOrchestrator:
    """Discover and export repo activity from all configured sources."""

    def __init__(self, sources: list[SourceConfig] | None = None):
        self.sources = sources if sources is not None else load_config()

    def run(
        self,
        days: int = 90,
        output_dir: Path = Path("."),
        dry_run: bool = False,
    ) -> Path | None:
        """Run all providers, write unified JSON export.

        Returns:
            Path to the written JSON file, or None on dry-run.
        """
        if not self.sources:
            print("No repo sources configured. Set GITLAB_TOKEN or GITHUB_TOKEN.")
            return None

        print(f"Repo sensing: {len(self.sources)} source(s) configured")
        results: dict[str, dict] = {}

        for source in self.sources:
            print(f"\n--- {source.provider} ({source.group_or_org}) ---")
            provider = _create_provider(source)

            if not provider.authenticate():
                print(f"  Skipping {source.provider}: authentication failed")
                continue

            repos = provider.discover_repos()
            print(f"  Found {len(repos)} repos")

            if dry_run:
                for repo in repos:
                    print(f"    {repo.full_path}")
                results[source.provider] = {
                    "url": source.url,
                    "group_or_org": source.group_or_org,
                    "repo_count": len(repos),
                }
                continue

            projects = []
            for i, repo in enumerate(repos, 1):
                print(f"  [{i}/{len(repos)}] {repo.full_path}...", end=" ", flush=True)
                activity = provider.get_activity(repo, days)
                commit_count = len(activity.commits)
                issue_count = len(activity.issues)
                print(f"{commit_count} commits, {issue_count} issues")
                projects.append({
                    "info": asdict(repo),
                    "activity": asdict(activity),
                })

            results[source.provider] = {
                "url": source.url,
                "group_or_org": source.group_or_org,
                "projects": projects,
            }

        if dry_run:
            print("\nDry-run complete.")
            return None

        # Build unified export
        all_projects = _flatten_projects(results)

        # Try to resolve raw author names via the sense correlator
        _resolve_fn: Callable[[str], str] | None = None
        try:
            from axiom.extensions.builtins.signals.correlator import Correlator
            correlator = Correlator()
            def _do_resolve(name: str) -> str:
                person = correlator.match_person(name)
                return person.name if person else name
            _resolve_fn = _do_resolve
        except Exception:
            pass  # No config available — use raw names
        resolve_author = _resolve_fn

        summary = _compute_summary(all_projects, days, resolve_author=resolve_author)

        export_data = {
            "exported_at": datetime.now(UTC).isoformat(),
            "time_window_days": days,
            "sources": results,
            "projects": all_projects,
            "summary": summary,
        }

        # Write output
        output_dir.mkdir(parents=True, exist_ok=True)
        date_str = datetime.now().strftime("%Y-%m-%d")
        filename = f"repo_export_{date_str}.json"
        filepath = output_dir / filename

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(export_data, f, indent=2, ensure_ascii=False, default=str)

        file_size = os.path.getsize(filepath)
        print(f"\nExported to: {filepath}")
        print(f"File size: {file_size / 1024:.1f} KB")

        _print_summary(summary, days)
        return filepath


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _flatten_projects(results: dict[str, dict]) -> list[dict]:
    """Merge projects from all sources into a single list."""
    flat: list[dict] = []
    for source_data in results.values():
        flat.extend(source_data.get("projects", []))
    return flat


def _compute_summary(
    projects: list[dict],
    days: int,
    resolve_author: Callable[[str], str] | None = None,
) -> dict:
    """Compute cross-project summary statistics.

    Args:
        resolve_author: Optional callable that maps a raw author name to its
            canonical form (e.g. "bbooth" → "Ben Booth").
    """
    summary: dict = {
        "total_commits_by_author": defaultdict(int),
        "stale_repos": [],
        "project_stats": [],
        "total_projects": len(projects),
        "total_commits": 0,
        "total_open_issues": 0,
        "total_open_mrs": 0,
        "total_issue_comments": 0,
    }

    for proj in projects:
        info = proj["info"]
        activity = proj.get("activity", {})
        full_path = info.get("full_path", "")

        # Commits by author
        for author, count in activity.get("contributor_summary", {}).items():
            canonical = resolve_author(author) if resolve_author else author
            summary["total_commits_by_author"][canonical] += count
            summary["total_commits"] += count

        # Stale repos
        commits = activity.get("commits", [])
        has_recent = any(is_within_days(c.get("created_at"), 30) for c in commits)
        if not has_recent:
            summary["stale_repos"].append(full_path)

        # Per-project stats
        issues = activity.get("issues", [])
        mrs = activity.get("merge_requests", [])
        open_issues = sum(1 for i in issues if not i.get("closed_at"))
        open_mrs = sum(1 for mr in mrs if not mr.get("merged_at") and mr.get("state") != "closed")
        issue_comments = len(activity.get("issue_comments", []))
        summary["total_open_issues"] += open_issues
        summary["total_open_mrs"] += open_mrs
        summary["total_issue_comments"] += issue_comments

        summary["project_stats"].append({
            "path": full_path,
            "commits": len(commits),
            "open_issues": open_issues,
            "open_mrs": open_mrs,
            "issue_comments": issue_comments,
        })

    # Sort
    summary["total_commits_by_author"] = dict(
        sorted(summary["total_commits_by_author"].items(), key=lambda x: -x[1])
    )
    return summary


def _print_summary(summary: dict, days: int) -> None:
    """Print a human-readable summary to the terminal."""
    print("\n" + "=" * 60)
    print("REPO EXPORT SUMMARY")
    print("=" * 60)

    print(f"\n  Projects: {summary['total_projects']}")
    print(f"  Commits (last {days} days): {summary['total_commits']}")
    print(f"  Open issues: {summary['total_open_issues']}")
    print(f"  Open MRs/PRs: {summary['total_open_mrs']}")
    print(f"  Issue comments: {summary['total_issue_comments']}")

    top_authors = list(summary["total_commits_by_author"].items())[:10]
    if top_authors:
        print(f"\n  Top contributors (last {days} days):")
        for author, count in top_authors:
            print(f"    {author}: {count} commits")

    if summary["stale_repos"]:
        print("\n  Stale repos (no commits in 30 days):")
        for repo in summary["stale_repos"]:
            print(f"    - {repo}")

    print("\n" + "=" * 60)


# ---------------------------------------------------------------------------
# CLI entry point (python -m tools.repo_sensing.orchestrator)
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export repo activity from all configured sources",
    )
    parser.add_argument(
        "--output-dir",
        default="exports",
        help="Directory for output file (default: exports)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="Time window in days (default: 90)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only discover and list repos, don't fetch activity",
    )
    args = parser.parse_args()

    orchestrator = RepoExportOrchestrator()
    orchestrator.run(
        days=args.days,
        output_dir=Path(args.output_dir),
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
