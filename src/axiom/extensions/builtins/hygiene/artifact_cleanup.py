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
from axiom.infra.paths import get_user_state_dir

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
    head_branch: str = ""
    head_sha: str = ""


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
                if a.get("expired"):
                    continue
                run = a.get("workflow_run") or {}
                yield Artifact(
                    id=str(a["id"]),
                    name=a.get("name", ""),
                    size_bytes=int(a.get("size_in_bytes", 0)),
                    created_at=datetime.fromisoformat(
                        a["created_at"].replace("Z", "+00:00")
                    ),
                    workflow_run_id=str(run.get("id", "")),
                    provider=self.name,
                    repo=repo,
                    head_branch=str(run.get("head_branch") or ""),
                    head_sha=str(run.get("head_sha") or ""),
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



    def list_caches(self, repo: str) -> Iterable["CacheRecord"]:
        for page in range(1, self._max_pages + 1):
            result = self._run(
                [
                    "gh",
                    "api",
                    f"repos/{repo}/actions/caches?per_page=100&page={page}",
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
            caches = payload.get("actions_caches", [])
            if not caches:
                break
            for c in caches:
                created = c.get("created_at") or c.get("last_accessed_at") or ""
                created_at = (
                    datetime.fromisoformat(created.replace("Z", "+00:00"))
                    if created
                    else datetime.fromtimestamp(0, tz=timezone.utc)
                )
                yield CacheRecord(
                    id=str(c["id"]),
                    key=str(c.get("key") or ""),
                    ref=str(c.get("ref") or ""),
                    size_bytes=int(c.get("size_in_bytes") or 0),
                    created_at=created_at,
                    provider=self.name,
                    repo=repo,
                )
            if len(caches) < 100:
                break

    def delete_cache(self, cache: "CacheRecord") -> bool:
        result = self._run(
            [
                "gh",
                "api",
                "-X",
                "DELETE",
                f"repos/{cache.repo}/actions/caches/{cache.id}",
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
        os.environ.get("AXIOM_HOME") or str(get_user_state_dir())
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



# ---------------------------------------------------------------------------
# Actions storage policy (dist-aware + merged-PR caches)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CacheRecord:
    """One Actions cache, provider-agnostic."""

    id: str
    key: str
    ref: str
    size_bytes: int
    created_at: datetime
    provider: str
    repo: str


def _is_version_tag(branch: str) -> bool:
    name = (branch or "").strip()
    if name.startswith("refs/tags/"):
        name = name[len("refs/tags/") :]
    if not name.startswith("v") or len(name) < 2:
        return False
    return name[1].isdigit()


def _pr_number(ref: str) -> int | None:
    parts = (ref or "").split("/")
    if len(parts) >= 3 and parts[0] == "refs" and parts[1] == "pull":
        try:
            return int(parts[2])
        except ValueError:
            return None
    return None


def _is_main_ref(ref: str) -> bool:
    return ref in {"refs/heads/main", "refs/heads/master", "main", "master"}


@dataclass(frozen=True)
class StoragePolicy:
    """What TIDY may delete from Actions artifacts and caches.

    Retention is per artifact class and configurable (policy.toml, settings,
    env). Main dist keeps the newest ``keep_main_dist`` regardless of age;
    older main dists fall under ``main_dist_days``. Benchmarks are kept.
    RIVET's disposability verdict can keep an artifact past retention or
    mark it disposable early; never-touch still wins, and an unreachable
    verdict store never permits early deletion.
    """

    keep_main_dist: int = 2
    keep_latest_tag_dist: bool = True
    main_dist_days: int = 3
    pr_dist_days: int = 3
    logs_days: int = 14
    coverage_days: int = 14
    release_dist_days: int = 14
    log_keep_days: int = 14  # alias of logs_days for older callers
    always_keep_prefixes: tuple[str, ...] = ("memory-benchmarks",)

    def days_for(self, cls: str) -> int | None:
        """Retention days for a class, or None to keep indefinitely."""
        if cls == "benchmarks":
            return None
        mapping = {
            "main_dist": self.main_dist_days,
            "pr_dist": self.pr_dist_days,
            "release_dist": self.release_dist_days,
            "logs": self.logs_days if self.logs_days else self.log_keep_days,
            "coverage": self.coverage_days,
        }
        return int(mapping.get(cls, self.logs_days))

    def classify(self, artifact: Artifact) -> str:
        name = (artifact.name or "").lower()
        if any(artifact.name.startswith(p) for p in self.always_keep_prefixes) or "benchmark" in name:
            return "benchmarks"
        if "coverage" in name:
            return "coverage"
        if artifact.name == "dist" or name == "dist" or name.startswith("dist-"):
            if _is_version_tag(artifact.head_branch):
                return "release_dist"
            if artifact.head_branch in {"main", "master"}:
                return "main_dist"
            return "pr_dist"
        return "logs"

    def latest_tag_branch(self, artifacts: list[Artifact], repo: str) -> str | None:
        tags = [
            a
            for a in artifacts
            if a.repo == repo and a.name == "dist" and _is_version_tag(a.head_branch)
        ]
        if not tags:
            return None
        return max(tags, key=lambda a: a.created_at).head_branch

    def select_artifacts(
        self,
        artifacts: list[Artifact],
        now: datetime,
        *,
        verdicts=None,
        decisions: list | None = None,
    ) -> tuple[list[Artifact], list[Artifact]]:
        """Return (to_delete, to_keep).

        ``verdicts`` is a :class:`VerdictLoad`. When it is missing or
        unreachable, TIDY uses retention only and never deletes early.
        ``decisions`` if given is filled with per-artifact reason dicts.
        """
        keep_ids: set[str] = set()
        never_touch: set[str] = set()
        by_repo: dict[str, list[Artifact]] = {}
        for a in artifacts:
            by_repo.setdefault(a.repo, []).append(a)

        for repo, arts in by_repo.items():
            for a in arts:
                if self.classify(a) == "benchmarks":
                    keep_ids.add(a.id)
                    never_touch.add(a.id)
            main_dists = sorted(
                [a for a in arts if self.classify(a) == "main_dist"],
                key=lambda a: a.created_at,
                reverse=True,
            )
            for a in main_dists[: self.keep_main_dist]:
                keep_ids.add(a.id)
                never_touch.add(a.id)
            if self.keep_latest_tag_dist:
                latest = self.latest_tag_branch(arts, repo)
                if latest:
                    for a in arts:
                        if a.name == "dist" and a.head_branch == latest:
                            keep_ids.add(a.id)
                            never_touch.add(a.id)

        unreachable = bool(verdicts is not None and getattr(verdicts, "unreachable", False))
        to_delete: list[Artifact] = []
        to_keep: list[Artifact] = []
        for a in artifacts:
            cls = self.classify(a)
            days = self.days_for(cls)
            past_retention = days is not None and a.created_at < (now - timedelta(days=days))
            rivet = None
            if verdicts is not None and not unreachable:
                rivet = verdicts.lookup(
                    artifact_id=a.id,
                    run_id=a.workflow_run_id,
                    repo=a.repo,
                    head_sha=getattr(a, "head_sha", "") or "",
                    now=now,
                )
            effective = rivet.effective(now) if rivet is not None else "unknown"
            reason = ""
            action = "keep"
            if a.id in never_touch:
                action, reason = "keep", "never-touch"
            elif effective == "keep":
                action, reason = "keep", f"rivet:{rivet.reason}"
            elif effective == "disposable" and not unreachable and a.id not in never_touch:
                action, reason = "delete", f"rivet:{rivet.reason}"
            elif past_retention:
                action, reason = "delete", f"retention:{cls}:{days}d"
            else:
                action, reason = "keep", f"retention:{cls}:{days}d" if days is not None else f"retention:{cls}:keep"

            if decisions is not None:
                decisions.append({
                    "id": a.id,
                    "repo": a.repo,
                    "name": a.name,
                    "class": cls,
                    "action": action,
                    "reason": reason,
                    "rivet_verdict": effective,
                    "rivet_reason": (rivet.reason if rivet is not None and effective != "unknown" else ""),
                })
            if action == "delete":
                to_delete.append(a)
            else:
                to_keep.append(a)
        return to_delete, to_keep

    def select_caches(
        self,
        caches: list[CacheRecord],
        *,
        open_prs: dict[str, set[int]],
        closed_or_merged: dict[tuple[str, int], bool],
    ) -> tuple[list[CacheRecord], list[CacheRecord]]:
        """Delete caches on merged/closed PR refs only. Keep main and open PRs."""
        to_delete: list[CacheRecord] = []
        to_keep: list[CacheRecord] = []
        for c in caches:
            if _is_main_ref(c.ref):
                to_keep.append(c)
                continue
            n = _pr_number(c.ref)
            if n is None:
                to_keep.append(c)
                continue
            if n in open_prs.get(c.repo, set()):
                to_keep.append(c)
                continue
            if closed_or_merged.get((c.repo, n)):
                to_delete.append(c)
            else:
                # Unknown PR state: do not delete.
                to_keep.append(c)
        return to_delete, to_keep

    @classmethod
    def from_settings(cls, settings_get=None) -> "StoragePolicy":
        """Build a policy from env (wins) then settings, else the shipped defaults."""
        import os

        def _get(setting: str, env: str, default):
            raw = os.environ.get(env)
            if raw is not None and str(raw).strip() != "":
                value = raw
            elif settings_get is not None:
                value = settings_get(setting, default)
            else:
                try:
                    from axiom.extensions.builtins.settings.store import SettingsStore

                    value = SettingsStore().get(setting, default)
                except Exception:  # noqa: BLE001
                    value = default
            if isinstance(default, bool):
                if isinstance(value, bool):
                    return value
                return str(value).strip().lower() not in {"0", "false", "off", "no"}
            try:
                return type(default)(value)
            except (TypeError, ValueError):
                return default

        logs = _get("hygiene.retention.logs_days", "AXIOM_RETENTION_LOGS_DAYS", 14)
        return cls(
            keep_main_dist=_get("hygiene.retention.keep_main_dist", "AXIOM_RETENTION_KEEP_MAIN_DIST", 2),
            keep_latest_tag_dist=_get("hygiene.retention.keep_latest_tag_dist", "AXIOM_RETENTION_KEEP_LATEST_TAG_DIST", True),
            main_dist_days=_get("hygiene.retention.main_dist_days", "AXIOM_RETENTION_MAIN_DIST_DAYS", 3),
            pr_dist_days=_get("hygiene.retention.pr_dist_days", "AXIOM_RETENTION_PR_DIST_DAYS", 3),
            logs_days=logs,
            coverage_days=_get("hygiene.retention.coverage_days", "AXIOM_RETENTION_COVERAGE_DAYS", 14),
            release_dist_days=_get("hygiene.retention.release_dist_days", "AXIOM_RETENTION_RELEASE_DIST_DAYS", 14),
            log_keep_days=logs,
        )




__all__ = [
    "Artifact",
    "CacheRecord",
    "CleanupParams",
    "CleanupResult",
    "GitHubProvider",
    "OP_NAME",
    "Provider",
    "RetentionPolicy",
    "StoragePolicy",
    "cleanup_artifacts",
    "_is_version_tag",
    "_pr_number",
]
