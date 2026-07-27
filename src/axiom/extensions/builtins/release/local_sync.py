# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Local-main sync — RIVET keeps every local default branch current.

RIVET's CI monitor (``ci_monitor``) reads *remote* pipeline state through
forge APIs. This module is its local-working-copy counterpart: on each
heartbeat it walks the workspace, fetches every repo's remote, and brings
each clean default branch up to date with its upstream.

Why RIVET and not TIDY
----------------------
Keeping ``main`` current is *acquiring upstream truth* — build/source
integrity, which is RIVET's domain ("makes the green"). It is **not**
janitorial cleanup (TIDY "removes the brown", ADR-046). Critically, the
sync is **non-destructive**: it only ever *fast-forwards*. It never merges,
rebases, resets, or deletes a ref. RIVET still never deletes refs — that
boundary is intact.

The state machine (per repo, per default branch)
------------------------------------------------
After ``git fetch``, the local default branch is compared to its
remote-tracking ref:

  * **up_to_date**     — even with the remote; nothing to do.
  * **behind**         — remote is strictly ahead → fast-forwardable.
        - clean tree → **fast_forwarded** (when ``apply``); otherwise the
          assessment is reported as ``behind`` (a dry-run "would ff").
        - default branch checked out *and* dirty → **behind_dirty**:
          surfaced, never fast-forwarded over uncommitted local edits.
  * **ahead**          — local has unpushed commits; nothing to pull.
  * **diverged**       — both sides moved → **surfaced, never touched**.
        This is the "branch with potential conflict against local changes"
        case the operator must resolve (rebase / merge / open a PR).
  * **no_remote / fetch_failed / no_default_branch / created** — edge
    conditions, each surfaced rather than crashing the sweep.

Host-agnostic by construction
------------------------------
``git fetch`` / fast-forward work identically against GitHub, GitLab
(gitlab.com or a self-hosted ``gitlab`` remote), Gitea,
or any other forge — there is no forge API on this path. The provider
label on each result (via :mod:`providers`) is purely informational, so
the operator can see at a glance which remotes are GitHub vs GitLab.

Unattended-safe: every git call goes through :func:`axiom.infra.git.run_git`,
whose env sets ``GIT_TERMINAL_PROMPT=0`` — a repo needing credentials fails
fast as ``fetch_failed`` instead of hanging the heartbeat on a prompt.

Discovery
---------
Repos are discovered one level under ``$AXI_WORKSPACE_ROOT`` (falling back
to the current directory) — the same convention TIDY's ``discover`` uses,
so a single workspace root drives both agents. New clones are picked up
automatically on the next tick.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from axiom.infra.git import (
    git_available,
    git_branch,
    git_is_dirty,
    git_remote_url,
    is_git_repo,
    run_git,
)

from .providers import detect_provider, parse_remote_url

__all__ = [
    "RepoSyncResult",
    "discover_workspace_repos",
    "provider_label",
    "resolve_default_branch",
    "sync_repo",
    "sync_workspace",
]


@dataclass
class RepoSyncResult:
    """Outcome of assessing (and optionally fast-forwarding) one repo."""

    repo: str  # repo basename, e.g. "CoreForge"
    path: str  # absolute clone path
    provider: str = "git"  # github | gitlab | gitea | git (informational)
    host: str = ""  # remote host, e.g. "github.com", "gitlab.example.org"
    remote_url: str = ""
    default_branch: str = ""
    current_branch: str = ""
    action: str = ""  # see module docstring for the vocabulary
    ahead: int = 0  # local-only commits on the default branch
    behind: int = 0  # remote-only commits on the default branch
    dirty: bool = False  # working tree had uncommitted changes
    detail: str = ""  # diagnostic text for failure/surface actions

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_workspace_repos(root: Path | str | None = None) -> list[Path]:
    """Git working clones one level under the workspace root.

    Resolution order for ``root``: the explicit argument, else
    ``$AXI_WORKSPACE_ROOT``, else the current working directory. Only
    directories with their own ``.git`` are returned (bare repos, which
    have no ``.git`` child, are skipped), sorted by path.
    """
    if root is None:
        env = os.environ.get("AXI_WORKSPACE_ROOT", "").strip()
        root = Path(env) if env else Path.cwd()
    root = Path(root)
    if not root.exists():
        return []
    return [
        entry
        for entry in sorted(root.iterdir())
        if entry.is_dir() and is_git_repo(entry)
    ]


# ---------------------------------------------------------------------------
# Provider labeling (informational; sync itself is host-agnostic)
# ---------------------------------------------------------------------------


def provider_label(remote_url: str) -> tuple[str, str]:
    """Map a remote URL to ``(provider_name, host)``.

    ``provider_name`` is ``github`` / ``gitlab`` / ``gitea`` when the host
    is recognized, else ``"git"`` (the sync works regardless). ``host`` is
    the parsed remote host, or ``""`` when unparseable.
    """
    ref = parse_remote_url(remote_url)
    host = ref.host if ref is not None else ""
    prov = detect_provider(remote_url)
    return (prov.name if prov is not None else "git"), host


# ---------------------------------------------------------------------------
# Git helpers (thin wrappers over infra.git; also used by tests)
# ---------------------------------------------------------------------------


def _git_head(repo: Path) -> str:
    return run_git(repo, "rev-parse", "HEAD").strip()


def _rev_parse(repo: Path, ref: str) -> str:
    return run_git(repo, "rev-parse", ref).strip()


def _git_branch(repo: Path) -> str:
    return git_branch(repo)


def _ref_exists(repo: Path, ref: str) -> bool:
    return bool(
        run_git(repo, "rev-parse", "--verify", "--quiet", ref, check=False).strip()
    )


def resolve_default_branch(repo: Path, remote: str = "origin") -> str | None:
    """The remote's default branch name, e.g. ``"main"``.

    Prefers the remote's advertised HEAD
    (``refs/remotes/<remote>/HEAD`` → branch); falls back to the first of
    ``main`` / ``master`` that exists as a remote-tracking ref. Returns
    None when none can be determined.
    """
    head = run_git(
        repo, "symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD", check=False
    ).strip()
    if head:
        return head.rsplit("/", 1)[-1]
    for candidate in ("main", "master"):
        if _ref_exists(repo, f"refs/remotes/{remote}/{candidate}"):
            return candidate
    return None


def _ahead_behind(repo: Path, local_ref: str, remote_ref: str) -> tuple[int, int]:
    """``(ahead, behind)`` of ``local_ref`` relative to ``remote_ref``."""
    out = run_git(
        repo,
        "rev-list",
        "--left-right",
        "--count",
        f"{local_ref}...{remote_ref}",
        check=False,
    ).strip()
    parts = out.split()
    if len(parts) != 2:
        return (0, 0)
    try:
        return (int(parts[0]), int(parts[1]))
    except ValueError:
        return (0, 0)


# ---------------------------------------------------------------------------
# Core sync
# ---------------------------------------------------------------------------


def sync_repo(repo: Path | str, *, apply: bool = True, remote: str = "origin") -> RepoSyncResult:
    """Assess one repo's default branch and, when ``apply``, fast-forward it.

    Non-destructive: the only mutation performed is a fast-forward of a
    clean, strictly-behind default branch. Everything else is surfaced via
    the returned :class:`RepoSyncResult` and left untouched.
    """
    repo = Path(repo)
    remote_url = git_remote_url(repo, remote)
    provider, host = provider_label(remote_url) if remote_url else ("git", "")
    res = RepoSyncResult(
        repo=repo.name,
        path=str(repo),
        provider=provider,
        host=host,
        remote_url=remote_url or "",
    )

    if not remote_url:
        res.action = "no_remote"
        return res

    try:
        run_git(repo, "fetch", "--quiet", remote)
    except subprocess.CalledProcessError as exc:
        res.action = "fetch_failed"
        res.detail = (exc.stderr or "").strip()[:200]
        return res

    default = resolve_default_branch(repo, remote)
    if not default:
        res.action = "no_default_branch"
        return res
    res.default_branch = default
    res.current_branch = git_branch(repo)

    remote_ref = f"{remote}/{default}"

    # No local copy of the default branch yet — create a tracking branch
    # (non-destructive: it adds a ref, never overwrites one).
    if not _ref_exists(repo, f"refs/heads/{default}"):
        if apply:
            run_git(repo, "branch", "--track", default, remote_ref, check=False)
            res.action = "created"
        else:
            res.action = "missing_local"
        return res

    ahead, behind = _ahead_behind(repo, default, remote_ref)
    res.ahead, res.behind = ahead, behind

    if ahead == 0 and behind == 0:
        res.action = "up_to_date"
        return res
    if ahead > 0 and behind > 0:
        res.action = "diverged"  # surfaced, never touched
        return res
    if ahead > 0:
        res.action = "ahead"  # unpushed local work; nothing to pull
        return res

    # behind only → fast-forwardable.
    on_default = res.current_branch == default
    res.dirty = git_is_dirty(repo) if on_default else False
    if on_default and res.dirty:
        res.action = "behind_dirty"  # don't fast-forward over local edits
        return res
    if not apply:
        res.action = "behind"  # would fast-forward
        return res

    try:
        if on_default:
            run_git(repo, "merge", "--ff-only", remote_ref)
        else:
            # Advance the non-checked-out local ref from the already-fetched
            # remote-tracking ref. Fetching from "." touches no network and
            # is fast-forward-only by default, so a non-ff update is refused
            # rather than forced.
            run_git(
                repo,
                "fetch",
                ".",
                f"refs/remotes/{remote_ref}:refs/heads/{default}",
            )
        res.action = "fast_forwarded"
    except subprocess.CalledProcessError as exc:
        res.action = "ff_failed"
        res.detail = (exc.stderr or "").strip()[:200]
    return res


def sync_workspace(
    root: Path | str | None = None, *, apply: bool = True
) -> list[RepoSyncResult]:
    """Sync every discovered repo's default branch. Never raises.

    One bad repo (permissions, corrupt git dir) degrades to an ``error``
    result and never sinks the rest of the sweep — same resilience contract
    as ``ci_monitor.check_pipelines``.
    """
    if not git_available():
        return []
    results: list[RepoSyncResult] = []
    for repo in discover_workspace_repos(root):
        try:
            results.append(sync_repo(repo, apply=apply, remote="origin"))
        except Exception as exc:  # noqa: BLE001 — resilience over precision here
            results.append(
                RepoSyncResult(
                    repo=repo.name,
                    path=str(repo),
                    action="error",
                    detail=str(exc)[:200],
                )
            )
    return results
