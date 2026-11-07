# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The repositories RIVET watches: one list, kept by the operator.

``<state>/agents/rivet/watched-repos.toml`` is the list. Pipelines, Actions
spend and the lander read it when they have no list of their own, and nothing
is assumed when it is empty. A built-in default repository is how a watcher
ends up looking at something nobody pushes to.
"""

from __future__ import annotations

from pathlib import Path

from axiom.infra.paths import get_user_state_dir

from .cross_repo_pr_watch import WatchedRepo, load_watched_repos


def watch_list_path(state_dir: Path | None = None) -> Path:
    return Path(state_dir or get_user_state_dir()) / "agents" / "rivet" / "watched-repos.toml"


def watched(state_dir: Path | None = None) -> list[WatchedRepo]:
    return load_watched_repos(watch_list_path(state_dir))


def names(state_dir: Path | None = None) -> tuple[str, ...]:
    """``owner/name`` for each watched repository."""
    return tuple(f"{w.repo_ref.owner}/{w.repo_ref.repo}" for w in watched(state_dir))


def monitor_entries(state_dir: Path | None = None) -> list[dict]:
    """The list in the shape ``ci_monitor`` reads."""
    return [
        {"url": f"https://{w.repo_ref.host}/{w.repo_ref.owner}/{w.repo_ref.repo}", "branch": w.ref}
        for w in watched(state_dir)
    ]


__all__ = ["monitor_entries", "names", "watch_list_path", "watched"]
