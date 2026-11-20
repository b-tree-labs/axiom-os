# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CI pipeline monitor — watches CI across configured repos.

Called on RIVET's heartbeat. Reads the watched-repo list from config,
resolves each entry to a provider (see ``providers``), and collects the
latest pipeline status. Failures degrade to "no signal" (None), never a
crash.

Watched-repo config
-------------------
The list of repos to watch is **config-driven**, not hardcoded. The
loader reads a TOML file with an array of ``[[repo]]`` tables:

    # ~/.axi/ci-repos.toml
    [[repo]]
    # auto-detect provider from a git remote URL (https or ssh)
    url = "git@github.com:b-tree-labs/axiom.git"

    [[repo]]
    # or describe the repo structurally
    provider  = "gitlab"             # github | gitlab | gitea | <registered>
    host      = "gitlab.example.org" # forge host (omit for github.com)
    project   = "group/sub/proj"     # owner[/subgroups]/repo path
    token_env = "MY_PROJECT_TOKEN"   # env var holding the API token

    [[repo]]
    # GitLab by numeric project ID (when the owner/repo path isn't handy)
    provider   = "gitlab"
    host       = "gitlab.example.org"
    project_id = "1234"              # addresses /api/v4/projects/<id>
    name       = "my-repo"           # optional display label
    token_env  = "MY_PROJECT_TOKEN"

    [[repo]]
    provider = "gitea"
    host     = "gitea.example.org"
    project  = "owner/repo"
    branch   = "main"                # optional; defaults to the repo default
    token_env = "GITEA_TOKEN"

Resolution order for the config path:
  1. ``$AXI_CI_REPOS`` — explicit path to a TOML file, if set.
  2. ``<user-state-dir>/ci-repos.toml`` — e.g. ``~/.axi/ci-repos.toml``.

When no config exists (or it is empty / malformed), the loader reads the one
watch list (``watch_list``). With that empty too, nothing is watched, and no
repository is assumed. Axiom never names a domain consumer here; a downstream
consumer layer adds its own GitLab / Gitea project by dropping a ``[[repo]]``
entry into either file.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from axiom.infra.paths import get_user_state_dir
from axiom.infra.toml_compat import load_toml

from .pipeline_status import PipelineStatus  # noqa: F401  (re-exported)
from .providers import RepoRef, detect_provider, get_provider
from .watch_integrity import (
    MISSING,
    LIVE,
    UNPARSEABLE,
    UNREACHABLE,
    Observation,
    Resolution,
    WatchTarget,
    observe,
)

__all__ = [
    "PipelineStatus",
    "check_pipelines",
    "get_build_status",
    "load_watched_repos",
    "observe_pipelines",
]

# Nothing is assumed. A repository named here is one nobody chose to watch.
_DEFAULT_REPOS: list[dict] = []


def _ci_repos_config_path():
    """Default config path: ``<user-state-dir>/ci-repos.toml``."""
    return get_user_state_dir() / "ci-repos.toml"


def load_watched_repos() -> list[dict]:
    """Load the watched-repo list from config.

    Honors ``$AXI_CI_REPOS`` (explicit file path) then the default
    ``<user-state-dir>/ci-repos.toml``. Reads the one watch list when no
    usable config is found — absent, empty, or malformed all fall back
    rather than crash.
    """
    env_path = os.environ.get("AXI_CI_REPOS", "").strip()
    path = env_path or _ci_repos_config_path()
    data = load_toml(path)  # returns {} on missing/malformed
    repos = data.get("repo")
    if isinstance(repos, list) and repos:
        return [r for r in repos if isinstance(r, dict)]
    from .watch_list import monitor_entries

    return monitor_entries() or list(_DEFAULT_REPOS)


def _repo_ref_from_entry(entry: dict) -> tuple[RepoRef | None, str | None]:
    """Resolve a config entry to ``(RepoRef, provider_name)``.

    A ``url`` entry is parsed + the provider auto-detected. A structured
    entry (``provider`` + ``host`` + ``project``) is assembled directly.
    Returns ``(None, None)`` when the entry is unusable.
    """
    url = entry.get("url")
    if url:
        ref = RepoRef.from_url(url)
        if ref is None:
            return None, None
        prov = detect_provider(url, override=entry.get("provider"))
        if prov is None:
            return None, None
        if entry.get("token_env"):
            ref = ref.with_token_env(entry["token_env"])
        if entry.get("branch"):
            ref = _with_branch(ref, entry["branch"])
        return ref, prov.name

    provider_name = entry.get("provider")
    if not provider_name:
        return None, None
    host = entry.get("host") or ("github.com" if provider_name == "github" else "")
    if not host:
        return None, None

    project = entry.get("project") or ""
    project_id = str(entry.get("project_id") or "").strip()
    if project_id:
        # Numeric-ID addressing (GitLab): the owner/repo path is optional;
        # a `name` (or the repo segment of `project`) gives a display label.
        owner, _, path_repo = project.rpartition("/")
        repo = entry.get("name") or path_repo or f"project-{project_id}"
        ref = RepoRef(
            host=host,
            owner=owner,
            repo=repo,
            base_url=f"https://{host}",
            branch=entry.get("branch", ""),
            token_env=entry.get("token_env", ""),
            project_id=project_id,
        )
        return ref, provider_name

    if "/" not in project:
        return None, None
    owner, _, repo = project.rpartition("/")
    ref = RepoRef(
        host=host,
        owner=owner,
        repo=repo,
        base_url=f"https://{host}",
        branch=entry.get("branch", ""),
        token_env=entry.get("token_env", ""),
    )
    return ref, provider_name


def _with_branch(ref: RepoRef, branch: str) -> RepoRef:
    from dataclasses import replace

    return replace(ref, branch=branch)


def _status_for_entry(entry: dict) -> PipelineStatus | None:
    """Resolve a single config entry to its latest pipeline status, or None."""
    ref, provider_name = _repo_ref_from_entry(entry)
    if ref is None or provider_name is None:
        return None
    provider = get_provider(provider_name)
    if provider is None:
        return None
    return provider.latest_pipeline(ref)


def _target(entry: dict, source: str) -> WatchTarget:
    ref, provider_name = _repo_ref_from_entry(entry)
    if ref is None:
        return WatchTarget(identifier=str(entry.get("url") or entry.get("project") or ""), source=source)
    name = f"{ref.owner}/{ref.repo}" if ref.owner else ref.repo
    return WatchTarget(identifier=name, source=source, provider=provider_name or "", host=ref.host)


def _watch_source(entries: list[dict]) -> str:
    if entries == _DEFAULT_REPOS:
        return "the built-in default"
    return os.environ.get("AXI_CI_REPOS", "").strip() or "ci-repos.toml"


def observe_pipelines() -> tuple[list[PipelineStatus], Observation]:
    """Every configured repo's status, and what the heartbeat may conclude.

    One bad repo never sinks the sweep, and it is never dropped either: it
    stays in the observation as a reason. An exception's message can carry a
    URL with a token in it, so only its type is kept.
    """
    entries = load_watched_repos()
    source = _watch_source(entries)
    results: list[PipelineStatus] = []
    resolutions: list[Resolution] = []
    for entry in entries:
        target = _target(entry, source)
        if _repo_ref_from_entry(entry)[0] is None:
            resolutions.append(Resolution(target, UNPARSEABLE))
            continue
        try:
            status = _status_for_entry(entry)
        except Exception as exc:  # noqa: BLE001 - any provider failure
            resolutions.append(Resolution(target, UNREACHABLE, why=type(exc).__name__))
            continue
        if status is None:
            resolutions.append(Resolution(target, MISSING, why="no pipeline came back"))
            continue
        results.append(status)
        resolutions.append(Resolution(target, LIVE))
    seen = observe(
        resolutions,
        pipelines_seen=len(results),
        failures_seen=sum(1 for p in results if p.status != "success"),
    )
    return results, seen


def check_pipelines() -> list[PipelineStatus]:
    """Check CI pipelines for all configured repos.

    A caller that concludes anything about health uses ``observe_pipelines``;
    this list alone cannot tell nothing wrong from nothing seen.
    """
    return observe_pipelines()[0]


def get_build_status() -> dict:
    """Get comprehensive build status across all configured repos."""
    from .mode import detect_mode

    mode = detect_mode()
    pipelines, seen = observe_pipelines()

    return {
        "mode": mode.to_dict(),
        "pipelines": [p.to_dict() for p in pipelines],
        "all_green": seen.all_green,
        "state": seen.state,
        "blind_reasons": list(seen.reasons),
        "checked_at": datetime.now(UTC).isoformat(),
    }
