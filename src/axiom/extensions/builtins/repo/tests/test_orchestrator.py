# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the repo sensing orchestrator.

Verifies multi-source orchestration, config detection, and JSON export shape.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from axiom.extensions.builtins.repo.base import RepoActivity, RepoInfo
from axiom.extensions.builtins.repo.config import SourceConfig, detect_sources, load_config
from axiom.extensions.builtins.repo.orchestrator import (
    RepoExportOrchestrator,
    _compute_summary,
    _flatten_projects,
)

# ---------------------------------------------------------------------------
# Config detection
# ---------------------------------------------------------------------------


class TestConfigDetection:
    @patch.dict(os.environ, {"GITLAB_TOKEN": "glpat-x", "GITHUB_TOKEN": "ghp_y"}, clear=False)
    def test_detect_both_sources(self):
        sources = detect_sources()
        names = [s.provider for s in sources]
        assert "gitlab" in names
        assert "github" in names

    @patch.dict(os.environ, {"GITLAB_TOKEN": "glpat-x"}, clear=False)
    def test_detect_gitlab_only(self):
        env = os.environ.copy()
        env.pop("GITHUB_TOKEN", None)
        with patch.dict(os.environ, env, clear=True):
            sources = detect_sources()
            assert len(sources) >= 1
            assert any(s.provider == "gitlab" for s in sources)

    def test_detect_no_tokens(self):
        env = os.environ.copy()
        env.pop("GITLAB_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
        with patch.dict(os.environ, env, clear=True):
            sources = detect_sources()
            assert sources == []

    def test_load_config_from_file(self, tmp_path):
        config_dir = tmp_path / ".neut"
        config_dir.mkdir()
        config_file = config_dir / "repo-sources.json"
        config_file.write_text(json.dumps([
            {
                "provider": "gitlab",
                "url": "https://custom-gitlab.example.com",
                "group_or_org": "my-group",
                "token_env": "MY_GITLAB_TOKEN",
            }
        ]))

        sources = load_config(tmp_path)
        assert len(sources) == 1
        assert sources[0].url == "https://custom-gitlab.example.com"

    def test_load_config_fallback_to_detect(self, tmp_path):
        """When no config file exists, falls back to auto-detection."""
        env = os.environ.copy()
        env.pop("GITLAB_TOKEN", None)
        env.pop("GITHUB_TOKEN", None)
        with patch.dict(os.environ, env, clear=True):
            sources = load_config(tmp_path)
            assert sources == []


# ---------------------------------------------------------------------------
# Flatten + summary
# ---------------------------------------------------------------------------


def _make_project(name: str, source: str, commits: int = 3) -> dict:
    """Create a fake project dict for testing."""
    return {
        "info": {
            "id": "1",
            "name": name,
            "full_path": f"org/{name}",
            "url": f"https://example.com/org/{name}",
            "default_branch": "main",
            "last_activity_at": None,
            "source": source,
        },
        "activity": {
            "commits": [
                {
                    "sha": f"abc{i}",
                    "author_name": "Dev",
                    "created_at": "2026-03-01T00:00:00+00:00",
                    "title": f"commit {i}",
                }
                for i in range(commits)
            ],
            "issues": [],
            "merge_requests": [],
            "branches": [],
            "labels": [],
            "milestones": [],
            "contributor_summary": {"Dev": commits},
            "issue_comments": [],
        },
    }


class TestFlattenProjects:
    def test_flattens_multiple_sources(self):
        results = {
            "gitlab": {
                "url": "https://gitlab.example.com",
                "group_or_org": "org",
                "projects": [_make_project("repo-a", "gitlab")],
            },
            "github": {
                "url": "https://github.com",
                "group_or_org": "org",
                "projects": [_make_project("repo-b", "github")],
            },
        }
        flat = _flatten_projects(results)
        assert len(flat) == 2
        names = {p["info"]["name"] for p in flat}
        assert names == {"repo-a", "repo-b"}


class TestComputeSummary:
    def test_summary_counts(self):
        projects = [
            _make_project("repo-a", "gitlab", commits=5),
            _make_project("repo-b", "github", commits=3),
        ]
        summary = _compute_summary(projects, days=90)

        assert summary["total_projects"] == 2
        assert summary["total_commits"] == 8
        assert summary["total_commits_by_author"]["Dev"] == 8

    def test_empty_projects(self):
        summary = _compute_summary([], days=90)
        assert summary["total_projects"] == 0
        assert summary["total_commits"] == 0

    def test_compute_summary_with_resolution(self):
        """Raw author names should merge under canonical name via resolve_author."""
        projects = [
            {
                "info": {"full_path": "org/repo-a"},
                "activity": {
                    "commits": [
                        {"sha": "a1", "author_name": "asmith", "created_at": "2026-03-01T00:00:00+00:00", "title": "c1"},
                    ],
                    "issues": [],
                    "merge_requests": [],
                    "contributor_summary": {"asmith": 3},
                    "issue_comments": [],
                },
            },
            {
                "info": {"full_path": "org/repo-b"},
                "activity": {
                    "commits": [
                        {"sha": "b1", "author_name": "Alice Smith", "created_at": "2026-03-01T00:00:00+00:00", "title": "c2"},
                    ],
                    "issues": [],
                    "merge_requests": [],
                    "contributor_summary": {"Alice Smith": 2},
                    "issue_comments": [],
                },
            },
        ]

        # Resolver maps "asmith" → "Alice Smith", passes through "Alice Smith"
        def resolver(name: str) -> str:
            mapping = {"asmith": "Alice Smith"}
            return mapping.get(name, name)

        summary = _compute_summary(projects, days=90, resolve_author=resolver)
        assert summary["total_commits_by_author"]["Alice Smith"] == 5
        assert "asmith" not in summary["total_commits_by_author"]


# ---------------------------------------------------------------------------
# Orchestrator integration
# ---------------------------------------------------------------------------


class TestOrchestrator:
    def test_run_no_sources(self, capsys):
        orchestrator = RepoExportOrchestrator(sources=[])
        result = orchestrator.run(days=90, output_dir=Path("/tmp"))
        assert result is None
        captured = capsys.readouterr()
        assert "No repo sources configured" in captured.out

    def test_run_dry_run(self, tmp_path):
        """Dry run should not write any files."""
        source = SourceConfig(
            provider="github",
            url="https://github.com",
            group_or_org="test-org",
            token_env="GITHUB_TOKEN",
        )

        mock_provider = MagicMock()
        mock_provider.authenticate.return_value = True
        mock_provider.discover_repos.return_value = [
            RepoInfo(
                id="1", name="repo", full_path="test-org/repo",
                url="https://github.com/test-org/repo",
                default_branch="main", last_activity_at=None, source="github",
            )
        ]

        orchestrator = RepoExportOrchestrator(sources=[source])
        with patch("axiom.extensions.builtins.repo.orchestrator._create_provider", return_value=mock_provider):
            result = orchestrator.run(days=90, output_dir=tmp_path, dry_run=True)

        assert result is None
        assert list(tmp_path.glob("*.json")) == []

    def test_run_full_export(self, tmp_path):
        """Full run should write a JSON file with the expected structure."""
        source = SourceConfig(
            provider="github",
            url="https://github.com",
            group_or_org="test-org",
            token_env="GITHUB_TOKEN",
        )

        repo_info = RepoInfo(
            id="1", name="repo", full_path="test-org/repo",
            url="https://github.com/test-org/repo",
            default_branch="main", last_activity_at=None, source="github",
        )

        mock_provider = MagicMock()
        mock_provider.authenticate.return_value = True
        mock_provider.discover_repos.return_value = [repo_info]
        mock_provider.get_activity.return_value = RepoActivity(
            commits=[{"sha": "abc", "author_name": "Dev", "created_at": "2026-03-01T00:00:00+00:00", "title": "test"}],
            contributor_summary={"Dev": 1},
        )

        orchestrator = RepoExportOrchestrator(sources=[source])
        with patch("axiom.extensions.builtins.repo.orchestrator._create_provider", return_value=mock_provider):
            result = orchestrator.run(days=90, output_dir=tmp_path)

        assert result is not None
        assert result.exists()

        data = json.loads(result.read_text())
        assert "exported_at" in data
        assert "sources" in data
        assert "projects" in data
        assert "summary" in data
        assert len(data["projects"]) == 1

    def test_auth_failure_skips_source(self, tmp_path):
        """If authentication fails, the source is skipped gracefully."""
        source = SourceConfig(
            provider="github",
            url="https://github.com",
            group_or_org="test-org",
            token_env="GITHUB_TOKEN",
        )

        mock_provider = MagicMock()
        mock_provider.authenticate.return_value = False

        orchestrator = RepoExportOrchestrator(sources=[source])
        with patch("axiom.extensions.builtins.repo.orchestrator._create_provider", return_value=mock_provider):
            result = orchestrator.run(days=90, output_dir=tmp_path)

        assert result is not None
        data = json.loads(result.read_text())
        assert data["sources"] == {}
        assert data["projects"] == []
