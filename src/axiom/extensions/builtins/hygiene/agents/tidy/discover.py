# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RIVET local-repo discovery for the local-RAG steward role.

Walks a workspace root, finds git repositories, and returns them as
candidates for graph + vector ingest. Honors a caller-supplied
exclusion list (e.g., for repositories under distinct IP regimes,
third-party clones, or scratch directories).

The discovery layer is policy + path-walking only. Actual ingestion
calls existing `axi rag` machinery — RIVET does not reinvent the
chunker, graph extractor, or store.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

DEFAULT_EXCLUDE_NAMES: frozenset[str] = frozenset()


@dataclass(frozen=True)
class DiscoveredRepo:
    path: Path
    head_sha: str | None


def _git_head(path: Path) -> str | None:
    from axiom.infra.git import safe_git_env
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=safe_git_env(path),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def discover_local_repos(
    root: Path | str,
    *,
    exclude_names: frozenset[str] | set[str] | None = None,
) -> list[DiscoveredRepo]:
    """Walk `root`, find git repos, return them.

    Skips dot-directories always; skips any directory whose name is in
    `exclude_names`. Does not recurse into a repo once found —
    submodule discovery is out of scope here.
    """
    root = Path(root)
    excluded = set(exclude_names) if exclude_names is not None else set(DEFAULT_EXCLUDE_NAMES)

    found: list[DiscoveredRepo] = []
    if not root.is_dir():
        return found

    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue
        if entry.name in excluded:
            continue
        if (entry / ".git").exists():
            found.append(DiscoveredRepo(path=entry, head_sha=_git_head(entry)))
            continue
        # Recurse one level — supports workspaces where repos are nested
        # under subgroups (e.g., org-name/repo-name layouts).
        found.extend(discover_local_repos(entry, exclude_names=excluded))

    return found
