# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for Publisher operating without a git repository.

Validates that publisher works correctly when installed standalone
(pip install example-consumer) without a .git/ directory present.
"""

import pytest

from axiom.extensions.builtins.publishing.config import (
    GitPolicy,
    ProviderConfig,
    PublisherConfig,
)
from axiom.extensions.builtins.publishing.engine import PublisherEngine
from axiom.extensions.builtins.publishing.git_integration import get_git_context


class TestGitContextFallback:
    """Test git context when git is unavailable."""

    def test_no_git_dir_returns_unavailable(self, tmp_path):
        """get_git_context returns git_available=False when no .git/."""
        ctx = get_git_context(tmp_path)
        assert ctx.git_available is False
        assert ctx.current_branch == "detached"
        assert ctx.commit_sha == "unknown"
        assert ctx.is_dirty is False  # Not dirty — just not tracked

    def test_real_repo_returns_available(self, repo_root):
        """Real repo returns git_available=True."""
        ctx = get_git_context(repo_root)
        assert ctx.git_available is True
        assert ctx.current_branch != "detached"


class TestEngineWithoutGit:
    """Test the Publisher engine operates correctly without git."""

    @pytest.fixture
    def no_git_engine(self, tmp_path):
        """Engine with repo_root pointing to a directory without .git/."""
        config = PublisherConfig(
            git=GitPolicy(require_clean=True),  # Would block if git were checked
            generation=ProviderConfig(provider="pandoc-docx"),
            storage=ProviderConfig(
                provider="local",
                settings={"base_dir": str(tmp_path / "published")},
            ),
            notification=ProviderConfig(provider="terminal"),
            repo_root=tmp_path,  # No .git/ here
        )
        return PublisherEngine(config)

    def test_publish_succeeds_without_git(self, no_git_engine, tmp_path):
        """Publishing works even when require_clean=True and no git."""
        source = tmp_path / "test-doc.md"
        source.write_text("# Test\n\nContent.\n")

        record = no_git_engine.publish(source, storage_override="local")
        assert record is not None
        assert record.version == "v1"
        assert record.commit_sha == "unknown"

    def test_state_stored_in_neut_dir(self, no_git_engine, tmp_path):
        """State files go under .neut/ when not in a git repo."""
        source = tmp_path / "state-doc.md"
        source.write_text("# State Test\n\nContent.\n")

        no_git_engine.publish(source, storage_override="local")

        pub_dir = tmp_path / ".neut" / "publisher"
        assert pub_dir.exists()
        assert (pub_dir / "publisher-state.json").exists()
        assert (pub_dir / "publisher-registry.json").exists()

    def test_generate_works_without_git(self, no_git_engine, tmp_path):
        """Local generation works without git."""
        source = tmp_path / "gen-doc.md"
        source.write_text("# Generate Test\n\nContent.\n")

        output = no_git_engine.generate(source, output_dir=tmp_path / "output")
        assert output.exists()
        assert output.suffix == ".docx"

    def test_status_works_without_git(self, no_git_engine):
        """Status query works without git."""
        docs = no_git_engine.status()
        assert docs == []

    def test_providers_work_without_git(self, no_git_engine):
        """Provider listing works without git."""
        providers = no_git_engine.list_providers()
        assert "generation" in providers
        assert "storage" in providers

    def test_version_increments_without_git(self, no_git_engine, tmp_path):
        """Version incrementing works correctly without git."""
        source = tmp_path / "versioned.md"
        source.write_text("# V1\n\nFirst.\n")

        r1 = no_git_engine.publish(source, storage_override="local")
        assert r1.version == "v1"

        source.write_text("# V2\n\nSecond.\n")
        r2 = no_git_engine.publish(source, storage_override="local")
        assert r2.version == "v2"
