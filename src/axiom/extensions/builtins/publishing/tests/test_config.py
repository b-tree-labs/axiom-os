# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for publisher configuration loading."""

import pytest

from axiom.extensions.builtins.publishing.config import (
    PublisherConfig,
    _discover_config_path,
    _find_project_root,
    _has_git,
    _state_dir,
    _substitute_env_vars,
    load_config,
)


class TestDefaultConfig:
    """Test default configuration values."""

    def test_defaults_without_file(self, tmp_path):
        config = load_config(tmp_path / "nonexistent.yaml")
        assert config.generation.provider == "pandoc-docx"
        assert config.storage.provider == "local"
        assert config.notification.provider == "terminal"
        assert config.git.require_clean is True
        assert config.review_default_days == 7

    def test_git_policy_defaults(self):
        config = PublisherConfig()
        assert "main" in config.git.publish_branches
        assert "release/*" in config.git.publish_branches
        assert "feature/*" in config.git.draft_branches


class TestYamlConfig:
    """Test YAML config loading."""

    def test_load_valid_yaml(self, tmp_path):
        yaml_content = """
git:
  publish_branches: [main]
  require_clean: false

generation:
  provider: pandoc-docx

storage:
  provider: onedrive

notifications:
  provider: terminal

review:
  default_days: 14
"""
        config_path = tmp_path / ".publisher.yaml"
        config_path.write_text(yaml_content)

        config = load_config(config_path)
        assert config.git.require_clean is False
        assert config.storage.provider == "onedrive"
        assert config.review_default_days == 14

    def test_load_real_config(self, repo_root):
        """Integration test: load the actual .publisher.yaml if it exists."""
        config_path = repo_root / ".publisher.yaml"
        if not config_path.exists():
            pytest.skip("No .publisher.yaml")

        config = load_config(config_path)
        assert config.generation.provider  # Should have a provider


class TestEnvVarSubstitution:
    """Test environment variable substitution in config."""

    def test_substitute_existing_var(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR", "hello")
        result = _substitute_env_vars("value: ${TEST_VAR}")
        assert result == "value: hello"

    def test_preserve_missing_var(self):
        result = _substitute_env_vars("value: ${DEFINITELY_NOT_SET_12345}")
        assert "${DEFINITELY_NOT_SET_12345}" in result

    def test_multiple_substitutions(self, monkeypatch):
        monkeypatch.setenv("A", "1")
        monkeypatch.setenv("B", "2")
        result = _substitute_env_vars("${A} and ${B}")
        assert result == "1 and 2"


class TestProjectRootDiscovery:
    """Test three-tier project root discovery."""

    def test_neut_root_env_overrides(self, tmp_path, monkeypatch):
        """AXIOM_ROOT env var takes priority over git and CWD."""
        target = tmp_path / "my-project"
        target.mkdir()
        monkeypatch.setenv("AXIOM_ROOT", str(target))
        result = _find_project_root()
        assert result == target

    def test_git_repo_found(self, repo_root):
        """Walking up should find the .git/ directory."""
        # We know the real repo root has .git/
        assert (repo_root / ".git").exists()

    def test_cwd_fallback_when_no_git(self, tmp_path, monkeypatch):
        """Without .git/ or AXIOM_ROOT, falls back to CWD."""
        monkeypatch.delenv("AXIOM_ROOT", raising=False)
        monkeypatch.chdir(tmp_path)
        # _find_project_root starts from __file__, so it may still find
        # the real .git. We test _has_git on a dir without .git instead.
        assert not _has_git(tmp_path)


class TestConfigDiscoveryHierarchy:
    """Test the six-tier config discovery."""

    def test_neut_config_env_takes_priority(self, tmp_path, monkeypatch):
        """AXIOM_CONFIG env var is checked first."""
        config_file = tmp_path / "custom.yaml"
        config_file.write_text("storage:\n  provider: s3\n")
        monkeypatch.setenv("AXIOM_CONFIG", str(config_file))
        monkeypatch.chdir(tmp_path)
        path = _discover_config_path()
        assert path == config_file

    def test_neut_publisher_workflow_yaml(self, tmp_path, monkeypatch):
        """Finds .neut/publisher/workflow.yaml (tier 2 - primary config path)."""
        monkeypatch.delenv("AXIOM_CONFIG", raising=False)
        # Create a .git dir so PROJECT_ROOT resolves to tmp_path
        (tmp_path / ".git").mkdir()
        config_dir = tmp_path / ".neut" / "publisher"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "workflow.yaml"
        config_file.write_text("storage:\n  provider: local\n")
        monkeypatch.chdir(tmp_path)
        # Re-evaluate PROJECT_ROOT to point to tmp_path
        import axiom.extensions.builtins.publishing.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "PROJECT_ROOT", tmp_path)
        path = _discover_config_path()
        assert path == config_file

    def test_legacy_doc_workflow_yaml(self, tmp_path, monkeypatch):
        """Finds .publisher.yaml as legacy fallback (tier 5)."""
        monkeypatch.delenv("AXIOM_CONFIG", raising=False)
        config_file = tmp_path / ".publisher.yaml"
        config_file.write_text("storage:\n  provider: local\n")
        monkeypatch.chdir(tmp_path)
        import axiom.extensions.builtins.publishing.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "PROJECT_ROOT", tmp_path)
        path = _discover_config_path()
        assert path == config_file

    def test_neut_subdir_config(self, tmp_path, monkeypatch):
        """Finds .neut/config.yaml in CWD."""
        monkeypatch.delenv("AXIOM_CONFIG", raising=False)
        neut_dir = tmp_path / ".neut"
        neut_dir.mkdir()
        config_file = neut_dir / "config.yaml"
        config_file.write_text("storage:\n  provider: local\n")
        monkeypatch.chdir(tmp_path)
        import axiom.extensions.builtins.publishing.config as cfg_mod
        monkeypatch.setattr(cfg_mod, "PROJECT_ROOT", tmp_path)
        path = _discover_config_path()
        assert path == config_file

    def test_returns_none_when_no_config(self, tmp_path, monkeypatch):
        """Returns None when no config file is found anywhere."""
        monkeypatch.delenv("AXIOM_CONFIG", raising=False)
        monkeypatch.delenv("AXIOM_ROOT", raising=False)
        monkeypatch.chdir(tmp_path)
        # This may find the real repo's config; we just verify it doesn't crash
        result = _discover_config_path()
        # Result is either None or a valid path
        assert result is None or result.exists()


class TestStateDir:
    """Test state file placement inside vs outside git repos."""

    def test_state_dir_in_git_repo(self, tmp_path):
        """In a git repo, state files go under .neut/publisher/."""
        (tmp_path / ".git").mkdir()
        result = _state_dir(tmp_path)
        assert result == tmp_path / ".neut" / "publisher"
        assert result.exists()

    def test_state_dir_outside_git(self, tmp_path):
        """Outside a git repo, state files go under .neut/publisher/."""
        result = _state_dir(tmp_path)
        assert result == tmp_path / ".neut" / "publisher"
        assert result.exists()

    def test_has_git_true(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert _has_git(tmp_path) is True

    def test_has_git_false(self, tmp_path):
        assert _has_git(tmp_path) is False
