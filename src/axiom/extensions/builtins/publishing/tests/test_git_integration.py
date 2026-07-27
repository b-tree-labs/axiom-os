# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for git integration."""

from axiom.extensions.builtins.publishing.git_integration import (
    SyncStatus,
    check_branch_policy,
    get_git_context,
)


class TestBranchPolicy:
    """Test branch policy enforcement."""

    def test_main_allows_publish(self):
        result = check_branch_policy(
            "main",
            publish_branches=["main", "release/*"],
            draft_branches=["feature/*", "dev"],
        )
        assert result == "publish"

    def test_release_branch_allows_publish(self):
        result = check_branch_policy(
            "release/v1.0",
            publish_branches=["main", "release/*"],
            draft_branches=["feature/*", "dev"],
        )
        assert result == "publish"

    def test_feature_branch_draft_only(self):
        result = check_branch_policy(
            "feature/add-auth",
            publish_branches=["main", "release/*"],
            draft_branches=["feature/*", "dev"],
        )
        assert result == "draft"

    def test_dev_branch_draft_only(self):
        result = check_branch_policy(
            "dev",
            publish_branches=["main", "release/*"],
            draft_branches=["feature/*", "dev"],
        )
        assert result == "draft"

    def test_unknown_branch_local_only(self):
        result = check_branch_policy(
            "experiment/random",
            publish_branches=["main", "release/*"],
            draft_branches=["feature/*", "dev"],
        )
        assert result == "local"


class TestGitContext:
    """Test git context detection."""

    def test_get_context_from_repo(self, repo_root):
        """Integration test: read actual git context."""
        ctx = get_git_context(repo_root)
        assert ctx.current_branch  # Should have a branch name
        assert ctx.commit_sha  # Should have a commit SHA
        assert isinstance(ctx.is_dirty, bool)

    def test_sync_status_enum(self):
        assert SyncStatus.IN_SYNC.value == "in_sync"
        assert SyncStatus.LOCAL_AHEAD.value == "local_ahead"
        assert SyncStatus.DIVERGED.value == "diverged"
