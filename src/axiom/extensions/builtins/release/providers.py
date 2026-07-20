# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CI-provider abstraction for RIVET's pipeline monitor.

RIVET watches CI across heterogeneous forges. This module decouples
"which forge" from "how to read its pipeline status" so the monitor can
see GitHub, GitLab (gitlab.com *or* any self-hosted instance), and Gitea
/ self-hosted forges without baking any single host into the code.

Three pieces:

  * :class:`RepoRef` — a parsed git remote (host, owner, repo, base URL,
    plus the env var that holds the API token). Built from a remote URL
    in either ``https://`` or ``git@host:owner/repo.git`` (ssh) form via
    :func:`parse_remote_url` / :meth:`RepoRef.from_url`.

  * :class:`CIProvider` — a ``runtime_checkable`` protocol: a ``name`` and
    ``latest_pipeline(repo_ref) -> PipelineStatus | None``. Each concrete
    provider returns ``None`` (never raises) when its dependency — the
    ``gh`` CLI, a GitLab token, a Gitea token, the ``requests`` library —
    is unavailable, so a missing tool degrades to "no signal" rather than
    a crash. Availability is checked through ``axiom.infra.capabilities``.

  * :func:`detect_provider` — maps a remote URL's host to a provider
    (``github.com`` → GitHub, any host containing ``gitlab`` → GitLab,
    ``gitea`` / ``forgejo`` / ``codeberg`` → Gitea), with an explicit
    ``override`` for self-hosted forges whose host name gives no hint.

Providers are looked up through a small registry (:func:`get_provider`,
:func:`register_provider`) so a consumer layer can contribute its own.

This module never names a domain consumer or a specific facility's host;
the watched-repo list lives in user config (see ``ci_monitor``).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable

from axiom.infra import capabilities

# PipelineStatus is owned here and re-exported by ci_monitor for back-compat.
from .pipeline_status import PipelineStatus

__all__ = [
    "PipelineStatus",
    "RepoRef",
    "CIProvider",
    "GitHubProvider",
    "GitLabProvider",
    "GiteaProvider",
    "parse_remote_url",
    "detect_provider",
    "get_provider",
    "register_provider",
    "unregister_provider",
]


# ---------------------------------------------------------------------------
# Remote-URL parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepoRef:
    """A parsed git remote: enough to address a provider's REST/CLI API."""

    host: str  # e.g. "github.com", "gitlab.example.org"
    owner: str  # owner or full group path, e.g. "b-tree-labs", "group/sub"
    repo: str  # repository name, e.g. "axiom"
    base_url: str  # scheme+host for REST endpoints, e.g. "https://github.com"
    branch: str = ""  # optional ref to scope a query (empty → provider default)
    token_env: str = ""  # env var holding the API token (provider-specific default)
    project_id: str = ""  # GitLab numeric project ID; when set, the API is
    # addressed by ID instead of the URL-encoded owner/repo path

    @property
    def project_path(self) -> str:
        """Full ``owner/repo`` path (handles subgroups in ``owner``)."""
        return f"{self.owner}/{self.repo}"

    def with_token_env(self, name: str) -> RepoRef:
        """Return a copy that reads its token from env var ``name``."""
        return replace(self, token_env=name)

    @classmethod
    def from_url(cls, url: str) -> RepoRef | None:
        """Parse a remote URL into a :class:`RepoRef`, or None if unparseable."""
        return parse_remote_url(url)


# https://host/owner[/subgroups...]/repo[.git]   (also http://)
_HTTPS_RE = re.compile(
    r"^(?P<scheme>https?)://(?:[^@/]+@)?(?P<host>[^/:]+)(?::\d+)?/(?P<path>.+?)(?:\.git)?/?$"
)
# git@host:owner[/subgroups...]/repo[.git]   (scp-like ssh)
_SCP_RE = re.compile(
    r"^(?:ssh://)?(?:[^@]+@)?(?P<host>[^/:]+):(?P<path>.+?)(?:\.git)?/?$"
)
# ssh://git@host[:port]/owner/repo.git
_SSH_RE = re.compile(
    r"^ssh://(?:[^@/]+@)?(?P<host>[^/:]+)(?::\d+)?/(?P<path>.+?)(?:\.git)?/?$"
)


def parse_remote_url(url: str) -> RepoRef | None:
    """Parse an https or ssh git remote URL into a :class:`RepoRef`.

    Returns None when the URL is empty, malformed, or lacks an
    ``owner/repo`` path (a host-only URL is not addressable).
    """
    if not url or not isinstance(url, str):
        return None
    url = url.strip()

    host = path = ""
    if url.startswith("ssh://"):
        m = _SSH_RE.match(url)
        if m:
            host, path = m.group("host"), m.group("path")
    elif url.startswith(("http://", "https://")):
        m = _HTTPS_RE.match(url)
        if m:
            host, path = m.group("host"), m.group("path")
    else:
        m = _SCP_RE.match(url)
        if m:
            host, path = m.group("host"), m.group("path")

    if not host or not path:
        return None

    segments = [s for s in path.split("/") if s]
    if len(segments) < 2:
        return None  # need at least owner + repo

    repo = segments[-1]
    owner = "/".join(segments[:-1])
    return RepoRef(
        host=host,
        owner=owner,
        repo=repo,
        base_url=f"https://{host}",
    )


# ---------------------------------------------------------------------------
# Provider protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class CIProvider(Protocol):
    """A CI forge RIVET can read pipeline status from."""

    name: str

    def latest_pipeline(self, repo_ref: RepoRef) -> PipelineStatus | None:
        """Latest pipeline/run status for ``repo_ref``, or None.

        Returns None — never raises — when the provider's dependency
        (CLI, token, or HTTP library) is unavailable or the call fails.
        """
        ...


# ---------------------------------------------------------------------------
# Concrete providers
# ---------------------------------------------------------------------------


def _import_requests():
    """Return the ``requests`` module, or None if unavailable."""
    try:
        import requests

        return requests
    except Exception:
        return None


def _resolve_token(repo_ref: RepoRef, default_env: str) -> str:
    """Read the API token from the ref's ``token_env`` or a default env var."""
    env_name = repo_ref.token_env or default_env
    return os.environ.get(env_name, "")


class GitHubProvider:
    """GitHub Actions, read through the authenticated ``gh`` CLI."""

    name = "github"

    def latest_pipeline(self, repo_ref: RepoRef) -> PipelineStatus | None:
        if not capabilities.is_available(capabilities.GH_CLI):
            return None
        try:
            result = subprocess.run(
                [
                    "gh",
                    "run",
                    "list",
                    "-R",
                    repo_ref.project_path,
                    "-L",
                    "1",
                    "--json",
                    "headBranch,status,conclusion,url",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode != 0:
                return None
            runs = json.loads(result.stdout)
            if not runs:
                return None
            run = runs[0]
            status = run.get("conclusion") or run.get("status") or "unknown"
            return PipelineStatus(
                repo=repo_ref.repo,
                provider="github",
                ref=run.get("headBranch", ""),
                status=status,
                url=run.get("url", ""),
            )
        except Exception:
            return None


class GitLabProvider:
    """GitLab CI, read through the REST API. Host is taken from the ref."""

    name = "gitlab"

    def latest_pipeline(self, repo_ref: RepoRef) -> PipelineStatus | None:
        # A repo may name a non-default token env var; prefer it when present.
        token = _resolve_token(repo_ref, "GITLAB_TOKEN")
        if not token:
            # Fall back to the capability probe (GITLAB_TOKEN env + glab
            # config). Skip gracefully if no token is discoverable at all.
            if not capabilities.is_available(capabilities.GITLAB_TOKEN):
                return None
            token = capabilities_gitlab_token()
        if not token:
            return None
        requests = _import_requests()
        if requests is None:
            return None
        try:
            # Address by numeric project ID when provided, else by the
            # URL-encoded owner/repo path.
            project = repo_ref.project_id or repo_ref.project_path.replace("/", "%2F")
            url = f"{repo_ref.base_url}/api/v4/projects/{project}/pipelines"
            resp = requests.get(
                url,
                headers={"PRIVATE-TOKEN": token},
                params={"per_page": 1, "order_by": "id", "sort": "desc"},
                timeout=10,
            )
            if resp.status_code != 200:
                return None
            pipelines = resp.json()
            if not pipelines:
                return None
            p = pipelines[0]
            return PipelineStatus(
                repo=repo_ref.repo,
                provider="gitlab",
                ref=p.get("ref", ""),
                status=p.get("status", "unknown"),
                url=p.get("web_url", ""),
            )
        except Exception:
            return None


class GiteaProvider:
    """Gitea / Forgejo, read through the REST combined commit-status API.

    Uses ``GET {base}/api/v1/repos/{owner}/{repo}/commits/{ref}/status`` —
    the standard combined-status endpoint that surfaces CI conclusion for
    the tip of a branch (defaults to the repo's default branch).
    """

    name = "gitea"

    def latest_pipeline(self, repo_ref: RepoRef) -> PipelineStatus | None:
        token = _resolve_token(repo_ref, "GITEA_TOKEN")
        if not token:
            return None
        requests = _import_requests()
        if requests is None:
            return None
        try:
            branch = repo_ref.branch or "main"
            url = (
                f"{repo_ref.base_url}/api/v1/repos/"
                f"{repo_ref.owner}/{repo_ref.repo}/commits/{branch}/status"
            )
            resp = requests.get(
                url,
                headers={"Authorization": f"token {token}"},
                timeout=10,
            )
            if resp.status_code != 200:
                return None
            data = resp.json() or {}
            return PipelineStatus(
                repo=repo_ref.repo,
                provider="gitea",
                ref=branch,
                status=data.get("state", "unknown"),
                url=data.get("target_url", ""),
            )
        except Exception:
            return None


def capabilities_gitlab_token() -> str:
    """Best-effort GitLab token from the glab CLI config (capability layer)."""
    # The GITLAB_TOKEN capability already probes env + glab config; this
    # mirrors its config read so the provider can use the token value, not
    # just the boolean. Kept narrow + exception-safe.
    try:
        from pathlib import Path

        import yaml

        cfg = Path.home() / ".config" / "glab-cli" / "config.yml"
        if not cfg.exists():
            return ""
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        for host_data in (data.get("hosts") or {}).values():
            if isinstance(host_data, dict) and host_data.get("token"):
                return str(host_data["token"])
    except Exception:
        return ""
    return ""


# ---------------------------------------------------------------------------
# Detection + registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, type] = {
    "github": GitHubProvider,
    "gitlab": GitLabProvider,
    "gitea": GiteaProvider,
}


def register_provider(name: str, provider_cls: type) -> None:
    """Register a provider class under ``name`` (pluggable by consumers)."""
    _REGISTRY[name] = provider_cls


def unregister_provider(name: str) -> None:
    """Remove a registered provider (no-op if absent)."""
    _REGISTRY.pop(name, None)


def get_provider(name: str) -> CIProvider | None:
    """Instantiate the provider registered under ``name``, or None."""
    cls = _REGISTRY.get(name)
    return cls() if cls is not None else None


def _provider_name_for_host(host: str) -> str | None:
    """Map a remote host to a provider name, or None if unrecognized."""
    h = host.lower()
    if h == "github.com" or h.endswith(".github.com"):
        return "github"
    if "gitlab" in h:
        return "gitlab"
    if "gitea" in h or "forgejo" in h or "codeberg" in h:
        return "gitea"
    return None


def detect_provider(remote_url: str, override: str | None = None) -> CIProvider | None:
    """Map a git remote URL's host to a provider instance, or None.

    ``override`` forces a provider by name (for self-hosted forges whose
    host gives no hint, e.g. a plain GitHub-Enterprise / Gitea hostname).
    An unknown override resolves to None.
    """
    if override:
        return get_provider(override)
    ref = parse_remote_url(remote_url)
    if ref is None:
        return None
    name = _provider_name_for_host(ref.host)
    if name is None:
        return None
    return get_provider(name)
