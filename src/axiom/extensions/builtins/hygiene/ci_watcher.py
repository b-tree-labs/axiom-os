# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tidy CI Watcher — auto-discovers CI providers from git remotes.

No hardcoded URLs or provider assumptions. Tidy reads `git remote -v`,
detects GitHub/GitLab/Gitea/Bitbucket from the URL, looks up credentials
from the Axiom connection system or environment, and queries each
provider's API.

Works on any local environment with git remotes configured.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class CIRunStatus:
    """Status of a CI/CD pipeline run."""

    provider: str = ""  # github, gitlab, etc.
    remote_name: str = ""  # origin, github, upstream, etc.
    run_id: str = ""
    branch: str = ""
    status: str = ""  # queued, in_progress, completed
    conclusion: str = ""  # success, failure, cancelled
    url: str = ""
    jobs: list[dict] = field(default_factory=list)
    failed_jobs: list[str] = field(default_factory=list)


@dataclass
class CIProvider:
    """A discovered CI provider from a git remote."""

    name: str  # remote name (origin, github, upstream)
    provider_type: str  # github, gitlab, gitea, bitbucket, unknown
    remote_url: str
    api_base: str = ""
    project_path: str = ""
    token: str = ""
    is_mirror: bool = False  # True if this remote is a read-only mirror

    @property
    def available(self) -> bool:
        return bool(self.token) or (self.provider_type == "github" and _has_gh_cli())


# ---------------------------------------------------------------------------
# Remote discovery — zero configuration
# ---------------------------------------------------------------------------


def discover_ci_providers(repo_dir: Path) -> list[CIProvider]:
    """Discover CI providers from git remotes. No hardcoding.

    Parses `git remote -v`, detects provider type from URL pattern,
    resolves credentials from environment or Axiom connections.
    """
    rc, out = _git(repo_dir, ["remote", "-v"])
    if rc != 0:
        return []

    seen = set()
    providers = []

    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2 or "(push)" not in line:
            continue  # Only look at push URLs, avoid duplicates

        remote_name = parts[0]
        remote_url = parts[1]

        if remote_name in seen:
            continue
        seen.add(remote_name)

        provider = _classify_remote(remote_name, remote_url)
        if provider.provider_type != "unknown":
            provider.token = _resolve_token(provider)
            providers.append(provider)

    # Detect mirror remotes: if origin is GitLab and there's a separate
    # github remote, the github remote is a read-only mirror.
    origin = next((p for p in providers if p.name == "origin"), None)
    if origin and origin.provider_type == "gitlab":
        for p in providers:
            if p.name != "origin" and p.provider_type == "github":
                p.is_mirror = True

    return providers


def _classify_remote(name: str, url: str) -> CIProvider:
    """Classify a git remote URL into a CI provider type."""
    url_lower = url.lower()

    # GitHub
    if "github.com" in url_lower:
        # Extract owner/repo from various URL formats
        match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
        project_path = match.group(1) if match else ""
        return CIProvider(
            name=name,
            provider_type="github",
            remote_url=url,
            api_base="https://api.github.com",
            project_path=project_path,
        )

    # GitLab (any instance — not just gitlab.com)
    if "gitlab" in url_lower:
        match = re.search(r"(https?://[^/]+)/(.+?)(?:\.git)?$", url)
        if match:
            base = match.group(1)
            project_path = match.group(2)
            return CIProvider(
                name=name,
                provider_type="gitlab",
                remote_url=url,
                api_base=f"{base}/api/v4",
                project_path=project_path,
            )

    # Gitea / Forgejo
    if "gitea" in url_lower or "forgejo" in url_lower or "codeberg" in url_lower:
        match = re.search(r"(https?://[^/]+)/(.+?)(?:\.git)?$", url)
        if match:
            return CIProvider(
                name=name,
                provider_type="gitea",
                remote_url=url,
                api_base=f"{match.group(1)}/api/v1",
                project_path=match.group(2),
            )

    # Bitbucket
    if "bitbucket" in url_lower:
        match = re.search(r"bitbucket\.org[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
        project_path = match.group(1) if match else ""
        return CIProvider(
            name=name,
            provider_type="bitbucket",
            remote_url=url,
            api_base="https://api.bitbucket.org/2.0",
            project_path=project_path,
        )

    return CIProvider(name=name, provider_type="unknown", remote_url=url)


def _resolve_token(provider: CIProvider) -> str:
    """Resolve API token for a CI provider.

    Resolution order:
    1. Provider-specific env var (GITHUB_TOKEN, GITLAB_TOKEN, etc.)
    2. Axiom connection system (axi connect)
    3. gh CLI auth (GitHub only)
    """
    env_vars = {
        "github": ["GITHUB_TOKEN", "GH_TOKEN"],
        "gitlab": ["GITLAB_TOKEN", "GITLAB_PRIVATE_TOKEN"],
        "gitea": ["GITEA_TOKEN"],
        "bitbucket": ["BITBUCKET_TOKEN"],
    }

    for var in env_vars.get(provider.provider_type, []):
        token = os.environ.get(var, "")
        if token:
            return token

    # Try Axiom connection system
    try:
        from axiom.infra.paths import get_user_state_dir

        env_file = get_user_state_dir().parent / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip("\"'")
                    if key in env_vars.get(provider.provider_type, []) and value:
                        return value
    except Exception:
        pass

    return ""


# ---------------------------------------------------------------------------
# Provider-specific CI checks
# ---------------------------------------------------------------------------


def _check_github(provider: CIProvider, repo_dir: Path) -> list[CIRunStatus]:
    """Query GitHub Actions via gh CLI or API."""
    if _has_gh_cli():
        return _check_github_via_cli(provider, repo_dir)
    if provider.token:
        return _check_github_via_api(provider)
    return []


def _check_github_via_cli(provider: CIProvider, repo_dir: Path) -> list[CIRunStatus]:
    """Use gh CLI (already authenticated)."""
    rc, out = _run(
        [
            "gh",
            "run",
            "list",
            "--limit",
            "5",
            "--json",
            "databaseId,headBranch,status,conclusion,url",
        ],
        cwd=repo_dir,
    )
    if rc != 0:
        return []

    try:
        runs = json.loads(out)
    except json.JSONDecodeError:
        return []

    results = []
    for run in runs:
        status = CIRunStatus(
            provider="github",
            remote_name=provider.name,
            run_id=str(run.get("databaseId", "")),
            branch=run.get("headBranch", ""),
            status=run.get("status", ""),
            conclusion=run.get("conclusion", ""),
            url=run.get("url", ""),
        )

        if status.conclusion == "failure":
            rc2, jobs_out = _run(
                [
                    "gh",
                    "run",
                    "view",
                    status.run_id,
                    "--json",
                    "jobs",
                    "--jq",
                    "[.jobs[] | {name: .name, conclusion: .conclusion}]",
                ],
                cwd=repo_dir,
            )
            if rc2 == 0:
                try:
                    jobs = json.loads(jobs_out)
                    status.jobs = jobs
                    status.failed_jobs = [
                        j["name"] for j in jobs if j.get("conclusion") == "failure"
                    ]
                except json.JSONDecodeError:
                    pass

        if status.status == "in_progress" or status.conclusion == "failure":
            results.append(status)

    return results


def _check_github_via_api(provider: CIProvider) -> list[CIRunStatus]:
    """Use GitHub REST API directly."""
    try:
        import requests

        resp = requests.get(
            f"{provider.api_base}/repos/{provider.project_path}/actions/runs",
            headers={
                "Authorization": f"Bearer {provider.token}",
                "Accept": "application/vnd.github+json",
            },
            params={"per_page": 5},
            timeout=10,
        )
        if resp.status_code != 200:
            return []

        results = []
        for run in resp.json().get("workflow_runs", []):
            status = CIRunStatus(
                provider="github",
                remote_name=provider.name,
                run_id=str(run.get("id", "")),
                branch=run.get("head_branch", ""),
                status=run.get("status", ""),
                conclusion=run.get("conclusion", ""),
                url=run.get("html_url", ""),
            )
            if status.status == "in_progress" or status.conclusion == "failure":
                results.append(status)
        return results
    except Exception:
        return []


def _check_gitlab(provider: CIProvider) -> list[CIRunStatus]:
    """Query GitLab CI/CD Pipelines API."""
    if not provider.token:
        return []

    encoded_path = provider.project_path.replace("/", "%2F")
    url = f"{provider.api_base}/projects/{encoded_path}/pipelines"

    try:
        import requests

        resp = requests.get(
            url,
            headers={"PRIVATE-TOKEN": provider.token},
            params={"per_page": 5, "order_by": "id", "sort": "desc"},
            timeout=10,
        )
        if resp.status_code != 200:
            return []

        results = []
        for pipeline in resp.json():
            gl_status = pipeline.get("status", "")
            status = CIRunStatus(
                provider="gitlab",
                remote_name=provider.name,
                run_id=str(pipeline.get("id", "")),
                branch=pipeline.get("ref", ""),
                status="completed"
                if gl_status in ("success", "failed", "canceled")
                else "in_progress",
                conclusion=gl_status,
                url=pipeline.get("web_url", ""),
            )
            if gl_status in ("failed", "running", "pending"):
                results.append(status)
        return results
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_ci_summary(repo_dir: Path) -> dict:
    """Auto-discover CI providers and get status across all remotes.

    This is the main entry point. Zero configuration needed — discovers
    everything from git remotes.

    Also includes local sweep health (see `local_sweep.py`); the remote
    pipeline state alone is not sufficient — pre-push-cached failures
    can sit in the working tree without ever reaching CI.
    """
    from .local_sweep import assess_local_sweep

    providers = discover_ci_providers(repo_dir)

    all_runs = []
    provider_info = []

    for provider in providers:
        provider_info.append(
            {
                "name": provider.name,
                "type": provider.provider_type,
                "project": provider.project_path,
                "authenticated": provider.available,
                "is_mirror": provider.is_mirror,
            }
        )

        if provider.provider_type == "github":
            all_runs.extend(_check_github(provider, repo_dir))
        elif provider.provider_type == "gitlab":
            all_runs.extend(_check_gitlab(provider))

    failed = [r for r in all_runs if r.conclusion in ("failure", "failed")]
    in_progress = [r for r in all_runs if r.status == "in_progress"]

    local_sweep = assess_local_sweep(repo_dir)
    healthy = len(failed) == 0 and local_sweep.healthy

    return {
        "healthy": healthy,
        "providers": provider_info,
        "failed_count": len(failed),
        "in_progress_count": len(in_progress),
        "failed_runs": [
            {
                "provider": r.provider,
                "remote": r.remote_name,
                "run_id": r.run_id,
                "branch": r.branch,
                "url": r.url,
                "failed_jobs": r.failed_jobs,
            }
            for r in failed
        ],
        "in_progress_runs": [
            {
                "provider": r.provider,
                "remote": r.remote_name,
                "run_id": r.run_id,
                "branch": r.branch,
                "url": r.url,
            }
            for r in in_progress
        ],
        "local_sweep": local_sweep.to_dict(),
    }


def run_ci_watch_cycle(repo_dir: Path) -> None:
    """Called by Tidy's heartbeat. Auto-discovers and checks all CI providers,
    and assesses local sweep health (Coverage Manifest entry, §4.1)."""
    from .local_sweep import run_local_sweep_cycle

    summary = get_ci_summary(repo_dir)

    for run in summary.get("failed_runs", []):
        jobs = ", ".join(run["failed_jobs"]) if run["failed_jobs"] else "check logs"
        log.warning(
            "CI FAILED [%s/%s]: %s on '%s' — %s — %s",
            run["provider"],
            run["remote"],
            run["run_id"],
            run["branch"],
            jobs,
            run["url"],
        )

    for run in summary.get("in_progress_runs", []):
        log.info(
            "CI in progress [%s/%s]: %s on '%s'",
            run["provider"],
            run["remote"],
            run["run_id"],
            run["branch"],
        )

    run_local_sweep_cycle(repo_dir)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _has_gh_cli() -> bool:
    return shutil.which("gh") is not None


def _run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    from axiom.infra.git import safe_git_env
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=safe_git_env(cwd if cwd is not None else Path.cwd()),
        )
        return result.returncode, result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return -1, ""


def _git(cwd: Path, args: list[str]) -> tuple[int, str]:
    return _run(["git"] + args, cwd=cwd)
