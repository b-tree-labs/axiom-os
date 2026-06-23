# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for repo sensing providers.

Mock all external API calls — these tests run without network access.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from axiom.extensions.builtins.repo.base import RepoActivity, RepoInfo, RepoSourceProvider

# ---------------------------------------------------------------------------
# ABC contract
# ---------------------------------------------------------------------------


class TestRepoSourceProviderABC:
    """Verify that providers must implement the full ABC."""

    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError):
            RepoSourceProvider()  # type: ignore[abstract]  # pylint: disable=abstract-class-instantiated

    def test_concrete_provider_must_implement_all(self):
        class Incomplete(RepoSourceProvider):
            @property
            def name(self):
                return "incomplete"

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]  # pylint: disable=abstract-class-instantiated


# ---------------------------------------------------------------------------
# RepoInfo / RepoActivity dataclasses
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_repo_info_creation(self):
        info = RepoInfo(
            id="42",
            name="my-repo",
            full_path="org/my-repo",
            url="https://example.com/org/my-repo",
            default_branch="main",
            last_activity_at="2025-01-01T00:00:00Z",
            source="github",
        )
        assert info.source == "github"
        assert info.full_path == "org/my-repo"

    def test_repo_activity_defaults(self):
        activity = RepoActivity()
        assert activity.commits == []
        assert activity.issues == []
        assert activity.merge_requests == []
        assert activity.contributor_summary == {}


# ---------------------------------------------------------------------------
# GitLab provider
# ---------------------------------------------------------------------------


class TestGitLabProvider:
    """Test GitLabProvider with mocked python-gitlab."""

    def _make_provider(self):
        from axiom.extensions.builtins.repo.providers.gitlab import GitLabProvider
        return GitLabProvider(
            url="https://gitlab.example.com",
            group="test-group",
            token_env="GITLAB_TOKEN",
        )

    @patch.dict(os.environ, {"GITLAB_TOKEN": ""}, clear=False)
    def test_authenticate_no_token(self):
        provider = self._make_provider()
        assert provider.authenticate() is False

    @patch.dict(os.environ, {"GITLAB_TOKEN": "glpat-fake"}, clear=False)
    def test_authenticate_success(self):
        provider = self._make_provider()
        mock_gl = MagicMock()
        mock_gl.user = MagicMock(username="testuser")

        with patch.dict("sys.modules", {"gitlab": MagicMock()}):
            import gitlab as gl_mod
            gl_mod.Gitlab = MagicMock(return_value=mock_gl)
            assert provider.authenticate() is True

    @patch.dict(os.environ, {"GITLAB_TOKEN": "glpat-fake"}, clear=False)
    def test_authenticate_failure(self):
        provider = self._make_provider()

        mock_gl = MagicMock()
        mock_gl.auth.side_effect = Exception("denied")

        with patch.dict("sys.modules", {"gitlab": MagicMock(), "gitlab.exceptions": MagicMock()}):
            import gitlab as gl_mod
            gl_mod.Gitlab = MagicMock(return_value=mock_gl)
            assert provider.authenticate() is False

    def test_name(self):
        provider = self._make_provider()
        assert provider.name == "gitlab"

    def test_discover_repos_not_authenticated(self):
        provider = self._make_provider()
        with pytest.raises(RuntimeError, match="authenticate"):
            provider.discover_repos()


# ---------------------------------------------------------------------------
# GitHub provider
# ---------------------------------------------------------------------------


class TestGitHubProvider:
    """Test GitHubProvider with mocked PyGithub."""

    def _make_provider(self):
        from axiom.extensions.builtins.repo.providers.github import GitHubProvider
        return GitHubProvider(org="test-org", token_env="GITHUB_TOKEN")

    @patch.dict(os.environ, {"GITHUB_TOKEN": ""}, clear=False)
    def test_authenticate_no_token(self):
        provider = self._make_provider()
        assert provider.authenticate() is False

    @patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_fake123"}, clear=False)
    def test_authenticate_success(self):
        provider = self._make_provider()
        mock_user = MagicMock()
        mock_user.login = "testuser"

        mock_github_mod = MagicMock()
        mock_client = MagicMock()
        mock_client.get_user.return_value = mock_user
        mock_github_mod.Github.return_value = mock_client

        with patch.dict("sys.modules", {"github": mock_github_mod}):
            assert provider.authenticate() is True

    def test_name(self):
        provider = self._make_provider()
        assert provider.name == "github"

    def test_discover_repos_not_authenticated(self):
        provider = self._make_provider()
        with pytest.raises(RuntimeError, match="authenticate"):
            provider.discover_repos()

    @patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_fake123"}, clear=False)
    def test_discover_repos(self):
        provider = self._make_provider()

        # Mock the client
        mock_repo = MagicMock()
        mock_repo.id = 1
        mock_repo.name = "test-repo"
        mock_repo.full_name = "test-org/test-repo"
        mock_repo.html_url = "https://github.com/test-org/test-repo"
        mock_repo.default_branch = "main"
        mock_repo.pushed_at = datetime(2025, 1, 1, tzinfo=UTC)

        mock_org = MagicMock()
        mock_org.get_repos.return_value = [mock_repo]

        mock_client = MagicMock()
        mock_client.get_organization.return_value = mock_org
        provider._client = mock_client

        repos = provider.discover_repos()
        assert len(repos) == 1
        assert repos[0].name == "test-repo"
        assert repos[0].source == "github"

    @patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_fake123"}, clear=False)
    def test_get_activity(self):
        provider = self._make_provider()

        # Mock the client and repo
        mock_commit = MagicMock()
        mock_commit.sha = "abc12345"
        mock_commit.commit.author.name = "Dev"
        mock_commit.commit.author.email = "dev@test.com"
        mock_commit.commit.author.date = datetime(2025, 6, 1, tzinfo=UTC)
        mock_commit.commit.message = "feat: test commit"

        mock_gh_repo = MagicMock()
        mock_gh_repo.get_commits.return_value = [mock_commit]
        mock_gh_repo.get_pulls.return_value = []
        mock_gh_repo.get_issues.return_value = []
        mock_gh_repo.get_labels.return_value = []
        mock_gh_repo.get_milestones.return_value = []
        mock_gh_repo.get_branches.return_value = []
        mock_gh_repo.default_branch = "main"

        mock_client = MagicMock()
        mock_client.get_repo.return_value = mock_gh_repo
        provider._client = mock_client

        repo_info = RepoInfo(
            id="1",
            name="test-repo",
            full_path="test-org/test-repo",
            url="https://github.com/test-org/test-repo",
            default_branch="main",
            last_activity_at=None,
            source="github",
        )

        activity = provider.get_activity(repo_info, days=30)
        assert len(activity.commits) == 1
        assert activity.commits[0]["author_name"] == "Dev"
        assert activity.contributor_summary == {"Dev": 1}


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------


class TestUtilities:
    def test_truncate(self):
        from axiom.extensions.builtins.repo.base import truncate

        assert truncate(None, 10) is None
        assert truncate("short", 10) == "short"
        assert truncate("a" * 20, 10) == "a" * 7 + "..."

    def test_parse_datetime(self):
        from axiom.extensions.builtins.repo.base import parse_datetime

        assert parse_datetime(None) is None
        assert parse_datetime("") is None
        dt = parse_datetime("2025-01-15T10:00:00Z")
        assert dt is not None
        assert dt.year == 2025

    def test_is_within_days(self):
        from axiom.extensions.builtins.repo.base import is_within_days

        assert is_within_days(None, 7) is False
        assert is_within_days("", 7) is False
        # A date very far in the past should not be within 7 days
        assert is_within_days("2020-01-01T00:00:00Z", 7) is False
        # Now should be within any window
        now_str = datetime.now(UTC).isoformat()
        assert is_within_days(now_str, 1) is True
