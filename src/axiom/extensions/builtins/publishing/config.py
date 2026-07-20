# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Configuration loader for Publisher.

Loads configuration via a multi-tier discovery hierarchy:
  1. Explicit path (--config flag or config_path argument)
  2. AXIOM_CONFIG environment variable
  3. .publisher.yaml in current directory
  4. .neut/config.yaml in current directory
  5. ~/.config/neut/config.yaml (user-level default)
  6. Built-in defaults

Project root discovery (for state files, registry, docs):
  1. AXIOM_ROOT environment variable
  2. Walk up for .git/ directory (repo context)
  3. Current working directory fallback
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _find_project_root() -> Path:
    """Find the project root using a three-tier strategy.

    1. AXIOM_ROOT env var (explicit override)
    2. Walk up from this file looking for .git/ (repo context)
    3. Fall back to current working directory
    """
    # Tier 1: explicit env var
    env_root = os.environ.get("AXIOM_ROOT")
    if env_root:
        return Path(env_root).resolve()

    # Tier 2: walk up looking for .git/
    path = Path(__file__).resolve().parent
    while path != path.parent:
        if (path / ".git").exists():
            return path
        path = path.parent

    # Tier 3: current working directory
    return Path.cwd()


def _has_git(root: Path) -> bool:
    """Check if a .git directory exists at the given root."""
    return (root / ".git").exists()


def _state_dir(root: Path) -> Path:
    """Return the directory for publisher state files.

    Always uses .neut/publisher/ to avoid polluting the project root.
    """
    state_dir = root / ".neut" / "publisher"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


PROJECT_ROOT = _find_project_root()


@dataclass
class GitPolicy:
    publish_branches: list[str] = field(default_factory=lambda: ["main", "release/*", "feat/*"])
    draft_branches: list[str] = field(default_factory=lambda: ["feature/*", "dev"])
    require_clean: bool = True
    require_pushed: bool = False  # Relaxed default for local dev


@dataclass
class ProviderConfig:
    """Configuration for a single provider category."""

    provider: str  # Name of the active provider
    settings: dict[str, Any] = field(default_factory=dict)


@dataclass
class PublisherConfig:
    """Complete Publisher configuration."""

    git: GitPolicy = field(default_factory=GitPolicy)
    generation: ProviderConfig = field(
        default_factory=lambda: ProviderConfig(provider="pandoc-docx")
    )
    storage: ProviderConfig = field(
        default_factory=lambda: ProviderConfig(provider="local")
    )
    feedback: ProviderConfig = field(
        default_factory=lambda: ProviderConfig(provider="docx-comments")
    )
    notification: ProviderConfig = field(
        default_factory=lambda: ProviderConfig(provider="terminal")
    )
    embedding_enabled: bool = False
    embedding: ProviderConfig = field(
        default_factory=lambda: ProviderConfig(provider="chromadb")
    )
    review_default_days: int = 7
    repo_root: Path = field(default_factory=lambda: PROJECT_ROOT)


def _discover_config_path() -> Path | None:
    """Walk the config discovery hierarchy, return first found path or None.

    1. AXIOM_CONFIG env var
    2. .publisher.yaml in CWD
    3. .neut/config.yaml in CWD
    4. ~/.config/neut/config.yaml
    """
    # Tier 1: env var
    env_config = os.environ.get("AXIOM_CONFIG")
    if env_config:
        p = Path(env_config)
        if p.exists():
            return p

    cwd = Path.cwd()

    # Tier 2: .neut/publisher/workflow.yaml in project root
    candidate = PROJECT_ROOT / ".neut" / "publisher" / "workflow.yaml"
    if candidate.exists():
        return candidate

    # Tier 3: .neut/config.yaml in CWD
    candidate = cwd / ".neut" / "config.yaml"
    if candidate.exists():
        return candidate

    # Tier 4: user-level config
    from axiom.infra.branding import get_branding
    candidate = Path.home() / ".config" / get_branding().cli_name / "config.yaml"
    if candidate.exists():
        return candidate

    # Tier 5: legacy .publisher.yaml in project root
    candidate = PROJECT_ROOT / ".publisher.yaml"
    if candidate.exists():
        return candidate

    return None


def load_config(config_path: Path | None = None) -> PublisherConfig:
    """Load Publisher configuration from YAML file.

    Falls back to defaults if no file exists.
    Environment variable substitution is supported via ${VAR_NAME} syntax.

    Args:
        config_path: Explicit config file path (--config flag).
                     If None, uses the discovery hierarchy.
    """
    if config_path is None:
        config_path = _discover_config_path()

    if config_path is None or not config_path.exists():
        return PublisherConfig()

    try:
        import yaml
    except ImportError:
        # PyYAML not installed — use defaults
        return PublisherConfig()

    try:
        raw = config_path.read_text(encoding="utf-8")
        # Substitute environment variables
        raw = _substitute_env_vars(raw)
        data = yaml.safe_load(raw) or {}
    except Exception:
        return PublisherConfig()

    config = PublisherConfig()

    # Git policies
    git_data = data.get("git", {})
    if git_data:
        config.git = GitPolicy(
            publish_branches=git_data.get("publish_branches", config.git.publish_branches),
            draft_branches=git_data.get("draft_branches", config.git.draft_branches),
            require_clean=git_data.get("require_clean", config.git.require_clean),
            require_pushed=git_data.get("require_pushed", config.git.require_pushed),
        )

    # Provider configs
    for category in ("generation", "storage", "feedback", "notification", "embedding"):
        cat_data = data.get(category if category != "notification" else "notifications", {})
        if cat_data:
            provider_name = cat_data.get("provider", getattr(config, category).provider)
            # Get provider-specific settings
            settings = cat_data.get(provider_name, {})
            setattr(config, category, ProviderConfig(
                provider=provider_name,
                settings=settings,
            ))

    # Embedding enabled flag
    embed_data = data.get("embedding", {})
    config.embedding_enabled = embed_data.get("enabled", False)

    # Review defaults
    review_data = data.get("review", {})
    config.review_default_days = review_data.get("default_days", 7)

    return config


def _substitute_env_vars(text: str) -> str:
    """Replace ${VAR_NAME} with environment variable values."""
    import re

    def replacer(match: re.Match[str]) -> str:
        var_name = match.group(1)
        return os.environ.get(var_name) or match.group(0)

    return re.sub(r"\$\{(\w+)\}", replacer, text)


# Backwards compatibility aliases
REPO_ROOT = PROJECT_ROOT
_find_repo_root = _find_project_root
