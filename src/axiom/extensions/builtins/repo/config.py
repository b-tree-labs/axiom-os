# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Configuration for repo sensing — auto-detection + optional override file.

Precedence:
  1. .neut/repo-sources.json  (written by ``axi config`` or by hand)
  2. Auto-detection from environment variables (GITLAB_TOKEN, GITHUB_TOKEN)
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from axiom.infra.state import atomic_write


@dataclass
class SourceConfig:
    """Configuration for a single repo source."""

    provider: str  # "gitlab" or "github"
    url: str
    group_or_org: str
    token_env: str  # env var name (not the value)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> SourceConfig:
        return cls(**data)


# ---------------------------------------------------------------------------
# Auto-detection
# ---------------------------------------------------------------------------


def detect_sources() -> list[SourceConfig]:
    """Auto-detect repo sources from environment variables."""
    sources: list[SourceConfig] = []

    from axiom.infra.connections import has_credential

    if has_credential("gitlab") or os.environ.get("GITLAB_TOKEN"):
        sources.append(SourceConfig(
            provider="gitlab",
            url=os.environ.get("GITLAB_URL", "https://gitlab.example.com"),
            group_or_org=os.environ.get("GITLAB_GROUP", "my-org"),
            token_env="GITLAB_TOKEN",
        ))

    if has_credential("github") or os.environ.get("GITHUB_TOKEN"):
        sources.append(SourceConfig(
            provider="github",
            url="https://github.com",
            group_or_org="UT-Computational-NE",
            token_env="GITHUB_TOKEN",
        ))

    return sources


# ---------------------------------------------------------------------------
# Config file support
# ---------------------------------------------------------------------------

_CONFIG_FILENAME = "repo-sources.json"


def _config_path(root: Path | None = None) -> Path:
    """Return the path to the repo-sources config file."""
    if root is None:
        root = Path.cwd()
    return root / ".neut" / _CONFIG_FILENAME


def load_config(root: Path | None = None) -> list[SourceConfig]:
    """Load sources from config file if it exists, else auto-detect."""
    path = _config_path(root)
    if path.exists():
        return _parse_config(path)
    return detect_sources()


def save_config(sources: list[SourceConfig], root: Path | None = None) -> Path:
    """Write sources to .neut/repo-sources.json.  Returns the path written."""
    path = _config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [s.to_dict() for s in sources]
    atomic_write(path, data)
    return path


def _parse_config(path: Path) -> list[SourceConfig]:
    """Parse a repo-sources.json file."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"Expected a JSON array in {path}")
    return [SourceConfig.from_dict(entry) for entry in raw]
