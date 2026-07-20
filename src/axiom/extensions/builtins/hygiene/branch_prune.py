# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TIDY's branch / remote-ref prune executor (ADR-046).

`git_signals.check_stale_branches` *detects* merged local branches but
never acts. Per ADR-046, TIDY owns the destructive git cleanup — and per
the M-O autonomy principle, detection alone is insufficient. This module
*executes*: it deletes merged local branches and merged remote refs,
under the ADR-045 D6 contract.

D6 wiring:
  - **Reversibility (D6.2):** every prune archives the ref under
    ``refs/tidy-archive/<kind>/<branch>`` *before* deleting, so each
    action is undoable via :func:`undo`. The action is declared
    ``reversible=True`` to the guard on that basis.
  - **Guarded volume (D6.3):** the batch runs through
    ``agent_action_guard.guarded_act`` at tier N. An over-limit batch
    downgrades to ``needs_confirmation`` (``volume_mode="confirm"``)
    rather than acting blindly; once the operator confirms, the caller
    re-runs with ``confirmed=True`` (``volume_mode="off"``).
  - **Merge confirmation:** a branch is a candidate only when merged into
    the default branch (``git branch [-r] --merged``). Protected branches
    and in-use / current branches are never touched.

The full learned-baseline breaker, novelty envelope, and act-then-notify
digest are the graduation-engine follow-on; this module runs at tier N
with the static confirm-downgrade.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from axiom.policy.agent_action_guard import AgentAction, guarded_act

from .git_signals import (
    PROTECTED_BRANCHES,
    _current_branch,
    _local_branches,
    _run,
)
from .worktrees import _is_ancestor_of_default, list_worktrees

ARCHIVE_NS = "refs/tidy-archive"

OP_LOCAL = "git.branch.delete"
OP_REMOTE = "git.remote_ref.delete"


@dataclass(frozen=True)
class PruneResult:
    """Outcome of a :func:`prune` run."""

    proceed: bool
    pruned: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    would_prune: list[str] = field(default_factory=list)
    reason: str = ""


# ---------------------------------------------------------------------------
# Default-branch resolution
# ---------------------------------------------------------------------------


def _remote_default(repo: Path, remote: str) -> tuple[str, str]:
    """Return ``(qualified_ref, branch_name)`` for the remote's default
    branch, e.g. ``("origin/main", "main")``. Falls back to ``<remote>/main``
    when ``<remote>/HEAD`` isn't set."""
    rc, out = _run(
        ["git", "symbolic-ref", "--short", "-q", f"refs/remotes/{remote}/HEAD"],
        cwd=repo,
    )
    qualified = out.strip() if rc == 0 and out.strip() else f"{remote}/main"
    name = qualified.split("/", 1)[1] if "/" in qualified else qualified
    return qualified, name


# ---------------------------------------------------------------------------
# Candidate listing
# ---------------------------------------------------------------------------


def list_merged_local(repo: Path) -> list[tuple[str, str]]:
    """``(branch, tip_sha)`` for local branches merged into the default
    branch, excluding protected branches and any branch checked out in a
    worktree (including the current branch). Mirrors
    ``git_signals.check_stale_branches`` but returns actionable pairs."""
    in_use: set[str] = {wt.branch for wt in list_worktrees(repo) if wt.branch}
    current = _current_branch(repo)
    if current:
        in_use.add(current)

    out: list[tuple[str, str]] = []
    for branch, sha in _local_branches(repo):
        if branch in PROTECTED_BRANCHES or branch in in_use:
            continue
        if not _is_ancestor_of_default(repo, sha):
            continue
        out.append((branch, sha))
    return out


def list_merged_remote(repo: Path, remote: str = "origin") -> list[tuple[str, str]]:
    """``(branch, tip_sha)`` for remote branches merged into the remote's
    default branch. Excludes the default branch itself, the ``HEAD``
    pointer, and protected names. Caller is responsible for a prior
    ``git fetch`` so the remote-tracking refs are current."""
    qualified, default_name = _remote_default(repo, remote)
    rc, out = _run(["git", "branch", "-r", "--merged", qualified], cwd=repo)
    if rc != 0:
        return []
    prefix = f"{remote}/"
    results: list[tuple[str, str]] = []
    for line in out.splitlines():
        name = line.strip()
        if not name or "->" in name:  # skip "origin/HEAD -> origin/main"
            continue
        if not name.startswith(prefix):
            continue
        branch = name[len(prefix):]
        if branch == default_name or branch in PROTECTED_BRANCHES:
            continue
        rc2, sha = _run(["git", "rev-parse", name], cwd=repo)
        if rc2 != 0:
            continue
        results.append((branch, sha.strip()))
    return results


# ---------------------------------------------------------------------------
# Reversible delete primitives
# ---------------------------------------------------------------------------


def _archive_ref(repo: Path, kind: str, branch: str, sha: str) -> bool:
    """Point ``refs/tidy-archive/<kind>/<branch>`` at ``sha`` so the
    branch is recoverable after deletion. Reversibility per D6.2."""
    ref = f"{ARCHIVE_NS}/{kind}/{branch}"
    rc, _ = _run(["git", "update-ref", ref, sha], cwd=repo)
    return rc == 0


def _prune_one_local(repo: Path, branch: str, sha: str) -> bool:
    if not _archive_ref(repo, "local", branch, sha):
        return False
    rc, _ = _run(["git", "branch", "-D", branch], cwd=repo)
    return rc == 0


_GITHUB_RE = re.compile(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$")


def _remote_url(repo: Path, remote: str) -> str:
    rc, out = _run(["git", "remote", "get-url", remote], cwd=repo)
    return out.strip() if rc == 0 else ""


def _github_slug(url: str) -> str | None:
    """Return ``owner/repo`` if ``url`` is a GitHub remote, else None."""
    m = _GITHUB_RE.search(url or "")
    return m.group(1) if m else None


def _gh_delete_ref(slug: str, branch: str) -> bool:
    """Delete a remote branch via ``gh api`` (uses gh's token directly, so
    it never blocks on a git credential prompt)."""
    import subprocess
    try:
        result = subprocess.run(
            ["gh", "api", "-X", "DELETE",
             f"repos/{slug}/git/refs/heads/{branch}"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _prune_one_remote(repo: Path, branch: str, sha: str, remote: str) -> bool:
    # Archive first (locally) so a failed delete still leaves an undo anchor.
    if not _archive_ref(repo, "remote", branch, sha):
        return False
    # GitHub remotes: delete via `gh api` — `git push --delete` over HTTPS
    # can block on an interactive credential prompt (osxkeychain), which
    # `gh` sidesteps with its own scoped token. Non-GitHub remotes fall back
    # to `git push --delete` (prompts disabled via safe_git_env, so it fails
    # fast rather than hanging).
    slug = _github_slug(_remote_url(repo, remote))
    if slug:
        return _gh_delete_ref(slug, branch)
    rc, _ = _run(["git", "push", remote, "--delete", branch], cwd=repo, timeout=30)
    return rc == 0


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def prune(
    repo: Path,
    *,
    state_dir: Path,
    remote: bool = False,
    remote_name: str = "origin",
    dry_run: bool = False,
    confirmed: bool = False,
) -> PruneResult:
    """Prune merged branches (local by default, remote when ``remote=True``)
    through the D6 guard at tier N.

    Returns a :class:`PruneResult`. When the batch exceeds the per-tick
    volume bound and ``confirmed`` is False, returns
    ``proceed=False, reason="needs_confirmation:..."`` with the candidate
    list in ``would_prune`` — the caller prompts and re-runs with
    ``confirmed=True``.
    """
    pairs = (
        list_merged_remote(repo, remote_name) if remote
        else list_merged_local(repo)
    )
    sha_by = dict(pairs)
    candidates = [b for b, _ in pairs]
    op_class = OP_REMOTE if remote else OP_LOCAL

    def do_one(branch: str) -> bool:
        sha = sha_by[branch]
        if remote:
            return _prune_one_remote(repo, branch, sha, remote_name)
        return _prune_one_local(repo, branch, sha)

    action = AgentAction(
        agent="tidy",
        op_class=op_class,
        name="prune_merged",
        candidates=candidates,
        reversible=True,
        metadata={"repo": str(repo), "remote": remote},
    )
    decision = guarded_act(
        action,
        do_one=do_one,
        state_dir=state_dir,
        dry_run=dry_run,
        volume_mode="off" if confirmed else "confirm",
    )
    return PruneResult(
        proceed=decision.proceed,
        pruned=list(decision.completed),
        failed=list(decision.refused),
        would_prune=list(decision.would_proceed),
        reason=decision.reason,
    )


def undo(repo: Path, branch: str, *, remote: bool = False,
         remote_name: str = "origin") -> bool:
    """Restore a pruned branch from its archive ref. Local restores the
    `refs/heads/<branch>`; remote pushes the archived sha back to the
    remote."""
    kind = "remote" if remote else "local"
    archive = f"{ARCHIVE_NS}/{kind}/{branch}"
    rc, sha = _run(["git", "rev-parse", "-q", "--verify", archive], cwd=repo)
    if rc != 0 or not sha.strip():
        return False
    sha = sha.strip()
    if remote:
        rc2, _ = _run(
            ["git", "push", remote_name, f"{sha}:refs/heads/{branch}"],
            cwd=repo, timeout=30,
        )
        return rc2 == 0
    rc2, _ = _run(["git", "update-ref", f"refs/heads/{branch}", sha], cwd=repo)
    return rc2 == 0
