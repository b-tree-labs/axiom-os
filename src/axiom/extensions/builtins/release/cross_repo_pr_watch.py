# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RIVET skill — watch trunk CI across a configured list of repos.

Closes a structural gap: RIVET previously watched only the repo it ran
from. A downstream consumer repo's ``main`` can go red and stay red
unnoticed when no skill iterates across the repos the operator cares
about. Typical consumers: a classroom-analytics deployment, an
agricultural ML platform, a domain extension's main repo.

This skill is intentionally thin:

  1. Load a list of watched repos from a TOML config (one source of
     truth; survives restarts; user-editable; auditable).
  2. For each repo, ask the appropriate CIProvider for the latest trunk
     pipeline status — provider-agnostic via the existing
     ``release.providers`` Factory/Protocol surface.
  3. Hand the snapshots to ``trunk_health.process_trunk_snapshots`` so
     the per-repo state machine (first-tick / persistent / clearance)
     does the de-duplication.

The skill never raises on a missing provider, a missing token, or a
network failure — degraded providers return ``None`` and the repo is
skipped for this tick. The user always gets *some* signal even when one
forge is down.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from axiom.extensions.builtins.release.pipeline_status import PipelineStatus
from axiom.extensions.builtins.release.providers import (
    RepoRef,
    detect_provider,
    parse_remote_url,
)
from axiom.extensions.builtins.release.trunk_health import (
    TrunkFinding,
    TrunkSnapshot,
    process_trunk_snapshots,
)


# tomllib is stdlib on 3.11+; fall back to the tomli backport for older
# pythons so the skill loads everywhere axiom runs.
if sys.version_info >= (3, 11):
    import tomllib as _toml
else:  # pragma: no cover - exercised on older interpreters
    import tomli as _toml  # type: ignore[no-redef]


__all__ = [
    "WatchedRepo",
    "load_watched_repos",
    "default_config_path",
    "cross_repo_pr_watch",
]


# ---------------------------------------------------------------------------
# Watch-list config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WatchedRepo:
    """One configured target for cross-repo trunk watching."""

    repo_ref: RepoRef
    ref: str = "main"
    """Default branch / ref to read pipeline status from."""


def default_config_path(home: Path | None = None) -> Path:
    """``~/.axi/agents/rivet/watched-repos.toml`` by default."""
    base = home or Path.home()
    return base / ".axi" / "agents" / "rivet" / "watched-repos.toml"


def load_watched_repos(path: Path) -> list[WatchedRepo]:
    """Parse the TOML config; return an empty list if missing/unparseable.

    Schema::

        [[repo]]
        url = "https://github.com/owner/name"
        ref = "main"        # optional, default "main"

    Bad entries (unparseable URLs) are silently dropped — the skill stays
    useful even when the operator typos one line.
    """
    if not path.exists():
        return []
    try:
        data = _toml.loads(path.read_text())
    except (OSError, _toml.TOMLDecodeError):
        return []
    out: list[WatchedRepo] = []
    for entry in data.get("repo", []):
        url = entry.get("url", "")
        ref = entry.get("ref", "main")
        repo_ref = parse_remote_url(url)
        if repo_ref is None:
            continue
        # If the config pins a ref, prefer it over the URL-derived branch.
        out.append(WatchedRepo(repo_ref=repo_ref, ref=ref))
    return out


# ---------------------------------------------------------------------------
# Provider Protocol — kept here so tests can stub without importing the
# full provider registry
# ---------------------------------------------------------------------------


class _CIProvider(Protocol):
    name: str

    def latest_pipeline(self, repo_ref: RepoRef) -> PipelineStatus | None: ...


# ---------------------------------------------------------------------------
# The skill
# ---------------------------------------------------------------------------


def cross_repo_pr_watch(
    targets: list[WatchedRepo],
    *,
    state_dir: Path,
    provider: _CIProvider | None = None,
    now: datetime | None = None,
) -> tuple[list[TrunkFinding], list[TrunkSnapshot]]:
    """Poll each target's trunk CI, route through the trunk_health state
    machine, return ``(findings, snapshots)``.

    Parameters
    ----------
    targets:
        Repos to watch this tick (typically from ``load_watched_repos``).
    state_dir:
        Persistent state root (the trunk_health JSON lives under
        ``<state_dir>/agents/rivet/trunk-health.json``).
    provider:
        Inject a stub in tests. In production, leave ``None`` and the
        per-repo CIProvider is detected from the URL host.
    now:
        Frozen-time override for tests.
    """
    snapshots: list[TrunkSnapshot] = []
    observed_at = now or datetime.now()
    for target in targets:
        # Resolve a provider per target (so a mixed GitHub + GitLab list
        # still works). Allow explicit injection for tests.
        prov: _CIProvider
        if provider is not None:
            prov = provider
        else:
            # detect_provider parses a full remote URL (owner/repo path
            # included); host-only would resolve to None.
            full_url = (
                f"{target.repo_ref.base_url}/{target.repo_ref.project_path}"
            )
            prov = detect_provider(full_url)  # type: ignore[assignment]
            if prov is None:
                continue
        try:
            status = prov.latest_pipeline(target.repo_ref)
        except Exception:
            # A provider that raises despite the Protocol contract is a
            # bug, but RIVET must not crash mid-fanout — skip this repo.
            status = None
        if status is None:
            continue
        snapshots.append(
            TrunkSnapshot(
                repo=target.repo_ref.project_path,
                ref=target.ref,
                status=status.status,
                url=status.url,
                observed_at=observed_at,
            )
        )
    findings = process_trunk_snapshots(
        snapshots, state_dir=state_dir, now=now
    )
    return findings, snapshots
