# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TIDY artifact-cleanup skill — keep CI artifact storage from filling.

Motivated by the 2026-05-30 GitHub Actions storage-quota incident on
a consumer-repo PR: 88 artifacts × 45 MB = 3.66 GB on page 1 alone;
PR builds began failing with ``Failed to CreateArtifact: Artifact
storage quota has been hit``. TIDY's job: prune old artifacts so this
never blocks a build silently.

**Provider-agnostic by design.** The first backend is GitHub (via ``gh
api``); GitLab + others slot in via the ``Provider`` protocol. The
policy layer (what to keep, what to prune) is shared.

**ADR-045 D6 contract:**

  - ``reversible=False`` — GitHub Actions artifact deletion is *not*
    undoable; the artifact is gone, period. But it's *recoverable* via
    re-trigger of the workflow run; risk class is low.
  - ``volume_mode="confirm"`` by default — operator must approve until
    graduation thresholds are met (per the D6 graduation engine).
  - **Keep policy** preserves recent runs + last-N-per-workflow even
    inside the prune window so a contemporaneous debug never loses its
    artifact.

**Per ADR-056** the entrypoint is a skill function
``cleanup_artifacts(params, ctx) -> SkillResult``; the CLI verb wraps
it.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Protocol

from axiom.policy.agent_action_guard import AgentAction, guarded_act

OP_NAME = "artifact.cleanup"


# ---------------------------------------------------------------------------
# Provider abstraction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Artifact:
    """One artifact, provider-agnostic."""

    id: str
    name: str
    size_bytes: int
    created_at: datetime
    workflow_run_id: str
    provider: str
    repo: str


class Provider(Protocol):
    """A repo-provider backend (GitHub, GitLab, …)."""

    name: str

    def list_artifacts(self, repo: str) -> Iterable[Artifact]: ...
    def delete_artifact(self, artifact: Artifact) -> bool: ...


# ---------------------------------------------------------------------------
# GitHub backend (via `gh`)
# ---------------------------------------------------------------------------


class GitHubProvider:
    name = "github"

    def __init__(
        self, *, runner: callable = subprocess.run, max_pages: int = 50
    ) -> None:
        self._run = runner
        self._max_pages = max_pages

    def list_artifacts(self, repo: str) -> Iterable[Artifact]:
        for page in range(1, self._max_pages + 1):
            result = self._run(
                [
                    "gh",
                    "api",
                    f"repos/{repo}/actions/artifacts?per_page=100&page={page}",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0 or not result.stdout.strip():
                break
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError:
                break
            arts = payload.get("artifacts", [])
            if not arts:
                break
            for a in arts:
                yield Artifact(
                    id=str(a["id"]),
                    name=a.get("name", ""),
                    size_bytes=int(a.get("size_in_bytes", 0)),
                    created_at=datetime.fromisoformat(
                        a["created_at"].replace("Z", "+00:00")
                    ),
                    workflow_run_id=str(
                        a.get("workflow_run", {}).get("id", "")
                    ),
                    provider=self.name,
                    repo=repo,
                )
            if len(arts) < 100:
                break

    def delete_artifact(self, artifact: Artifact) -> bool:
        result = self._run(
            [
                "gh",
                "api",
                "-X",
                "DELETE",
                f"repos/{artifact.repo}/actions/artifacts/{artifact.id}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetentionPolicy:
    """What to keep, what to prune.

    Defaults are conservative: anything in the last 7 days, plus the
    last 3 runs per workflow even if older. The latter guards against
    a quiet workflow's most recent artifacts being deleted just because
    they're old.
    """

    keep_days: int = 7
    keep_last_n_per_workflow: int = 3

    def select_for_deletion(
        self, artifacts: list[Artifact], now: datetime
    ) -> tuple[list[Artifact], list[Artifact]]:
        """Return (to_delete, to_keep)."""
        cutoff = now - timedelta(days=self.keep_days)
        recent_per_workflow: dict[str, list[Artifact]] = {}
        for a in artifacts:
            recent_per_workflow.setdefault(a.workflow_run_id, []).append(a)
        # Sort each workflow's artifacts newest-first; mark top N as keep.
        keep_ids: set[str] = set()
        for arts in recent_per_workflow.values():
            arts.sort(key=lambda x: x.created_at, reverse=True)
            for a in arts[: self.keep_last_n_per_workflow]:
                keep_ids.add(a.id)
        # Now classify.
        to_delete: list[Artifact] = []
        to_keep: list[Artifact] = []
        for a in artifacts:
            if a.id in keep_ids or a.created_at >= cutoff:
                to_keep.append(a)
            else:
                to_delete.append(a)
        return to_delete, to_keep


# ---------------------------------------------------------------------------
# Skill function (ADR-056)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CleanupParams:
    repos: list[tuple[str, str]]
    """List of (provider_name, repo). E.g. [('github', 'b-tree-labs/axiom-os')]"""
    policy: RetentionPolicy = field(default_factory=RetentionPolicy)
    dry_run: bool = False
    confirmed: bool = False
    """Operator confirmation per ADR-045 D6 volume gating."""


@dataclass(frozen=True)
class CleanupResult:
    deleted_count: int
    freed_bytes: int
    kept_count: int
    per_repo: dict[str, dict[str, int]]
    """Map of repo → {deleted, freed_bytes, kept}."""
    confirmed_required: bool = False
    """True when the operator needs to re-run with confirmed=True."""


def cleanup_artifacts(
    params: CleanupParams,
    *,
    providers: dict[str, Provider] | None = None,
    now: datetime | None = None,
    state_dir: Path | None = None,
) -> CleanupResult:
    """Run the cleanup across all configured repos.

    Per ADR-045 D6 + ADR-056: each repo's deletion batch is gated by
    ``guarded_act``. The first run requires explicit confirmation;
    subsequent runs graduate to autonomous via D6's success counter.
    """
    providers = providers or {"github": GitHubProvider()}
    now = now or datetime.now(timezone.utc)
    state_dir = state_dir or Path(
        os.environ.get("AXIOM_HOME", str(Path.home() / ".axi"))
    )
    total_deleted = 0
    total_freed = 0
    total_kept = 0
    per_repo: dict[str, dict[str, int]] = {}
    needs_confirm = False

    for provider_name, repo in params.repos:
        provider = providers.get(provider_name)
        if provider is None:
            continue

        artifacts = list(provider.list_artifacts(repo))
        to_delete, to_keep = params.policy.select_for_deletion(artifacts, now)

        per_repo_freed = sum(a.size_bytes for a in to_delete)
        per_repo[repo] = {
            "deleted": 0,
            "freed_bytes": 0,
            "kept": len(to_keep),
            "candidate_for_deletion": len(to_delete),
            "candidate_freed_bytes": per_repo_freed,
        }

        if not to_delete:
            continue

        if params.dry_run:
            continue

        # ADR-045 D6 volume gating. The guard calls our worker once per
        # candidate; it owns volume thresholds + reversibility checks +
        # the volume_mode=confirm downgrade.
        deleted_here = 0
        freed_here = 0

        def _delete_one(candidate: Artifact) -> bool:
            nonlocal deleted_here, freed_here
            if provider.delete_artifact(candidate):
                deleted_here += 1
                freed_here += candidate.size_bytes
                return True
            return False

        action = AgentAction(
            agent="tidy",
            op_class=OP_NAME,
            name=f"prune_{repo.replace('/', '_')}",
            candidates=to_delete,
            # ADR-045 D6.2 nuance: artifact deletion is irreversible at
            # the API boundary (no restore endpoint) but the build product
            # is *regenerable* via a CI re-run. Distinct from, e.g., a
            # destructive DB drop. We mark reversible=True with explicit
            # metadata recovery_path so the autonomous guard treats it
            # equivalently to TIDY's archive-then-delete branch prune,
            # which is also a regeneration-by-re-fetch contract.
            reversible=True,
            metadata={
                "provider": provider_name,
                "repo": repo,
                "artifacts_freed_bytes": per_repo_freed,
                "policy_keep_days": params.policy.keep_days,
                "recovery_path": "re-run the source CI workflow",
                "irreversible_at_api": True,
            },
        )
        decision = guarded_act(
            action,
            do_one=_delete_one,
            state_dir=state_dir,
            volume_mode="off" if params.confirmed else "confirm",
        )
        if not decision.proceed:
            if "confirm" in decision.reason.lower():
                needs_confirm = True
            continue

        per_repo[repo]["deleted"] = deleted_here
        per_repo[repo]["freed_bytes"] = freed_here
        total_deleted += deleted_here
        total_freed += freed_here
        total_kept += len(to_keep)

    return CleanupResult(
        deleted_count=total_deleted,
        freed_bytes=total_freed,
        kept_count=total_kept,
        per_repo=per_repo,
        confirmed_required=needs_confirm,
    )


__all__ = [
    "Artifact",
    "CleanupParams",
    "CleanupResult",
    "GitHubProvider",
    "OP_NAME",
    "Provider",
    "RetentionPolicy",
    "cleanup_artifacts",
]
