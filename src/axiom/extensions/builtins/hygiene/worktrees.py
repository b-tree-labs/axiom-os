# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Stale-worktree discovery for TIDY.

A worktree is *stale* only when TIDY can cite hard evidence — never on
"looks old" alone. The verdict carries a list of fired signals so TIDY
can show its work in the proposal.

Strong signals (any one fires `is_stale=True`):

  S1  Worktree directory does not exist on disk.
      `git worktree prune` would clean this up; safe regardless of branch
      state.

  S2  Branch has been deleted on origin.
      `git ls-remote origin refs/heads/<branch>` returns no rows.
      Strong indicator the work has been wrapped up upstream (squash-
      merged PR, abandoned branch, etc.).

  S3  Branch tip is an ancestor of the repo's default branch.
      `git merge-base --is-ancestor <head> origin/main` returns 0. The
      worktree's commits are already in main; nothing left to lose.

  S4  A PR for the branch is merged or closed.
      `gh pr list --head <branch> --state all` reports merged/closed.
      Catches squash-merges where S3 wouldn't fire.

The "dirty" check is a *floor*, not a signal: if the worktree has
uncommitted or untracked work (excluding the nits in `_NIT_PATHS`),
TIDY downgrades to advisory and refuses force-prune by default.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_BRANCH_CANDIDATES: tuple[str, ...] = (
    "origin/main",
    "origin/master",
    "main",
    "master",
)

_NIT_PATHS: frozenset[str] = frozenset(
    {".DS_Store", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)


@dataclass(frozen=True)
class WorktreeInfo:
    """One row of `git worktree list --porcelain`."""

    path: Path
    branch: str  # e.g. "feat/foo"; empty if detached
    head_sha: str
    locked: bool = False
    prunable_per_git: bool = False  # git's own --porcelain "prunable" flag


@dataclass
class StalenessVerdict:
    worktree: WorktreeInfo
    is_stale: bool = False
    is_dirty: bool = False  # has uncommitted or non-nit untracked content
    can_force_prune: bool = True  # False when dirty and not all nits
    reasons: list[str] = field(default_factory=list)


def _run(args: list[str], cwd: Path | None = None, timeout: int = 10) -> tuple[int, str]:
    from axiom.infra.git import safe_git_env
    try:
        result = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=safe_git_env(cwd if cwd is not None else Path.cwd()),
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return result.returncode, result.stdout


def list_worktrees(repo: Path) -> list[WorktreeInfo]:
    """Parse `git worktree list --porcelain` for `repo`."""

    rc, out = _run(["git", "worktree", "list", "--porcelain"], cwd=repo)
    if rc != 0:
        return []

    worktrees: list[WorktreeInfo] = []
    cur: dict[str, object] = {}

    def flush() -> None:
        if "path" not in cur:
            return
        worktrees.append(
            WorktreeInfo(
                path=Path(str(cur["path"])),
                branch=str(cur.get("branch", "")),
                head_sha=str(cur.get("HEAD", "")),
                locked=bool(cur.get("locked", False)),
                prunable_per_git=bool(cur.get("prunable", False)),
            )
        )
        cur.clear()

    for raw in out.splitlines():
        if not raw:
            flush()
            continue
        token, _, value = raw.partition(" ")
        if token == "worktree":
            cur["path"] = value
        elif token == "HEAD":
            cur["HEAD"] = value
        elif token == "branch":
            cur["branch"] = value.removeprefix("refs/heads/")
        elif token == "locked":
            cur["locked"] = True
        elif token == "prunable":
            cur["prunable"] = True
        elif token == "detached":
            cur["branch"] = ""
    flush()

    return worktrees


def _branch_exists_on_origin(repo: Path, branch: str) -> bool:
    if not branch:
        return True  # detached HEAD: don't claim "deleted upstream"
    rc, out = _run(
        ["git", "ls-remote", "--heads", "origin", branch], cwd=repo, timeout=15
    )
    return rc == 0 and bool(out.strip())


def _is_ancestor_of_default(repo: Path, head: str) -> bool:
    if not head:
        return False
    for candidate in DEFAULT_BRANCH_CANDIDATES:
        rc, _ = _run(
            ["git", "merge-base", "--is-ancestor", head, candidate], cwd=repo
        )
        if rc == 0:
            return True
    return False


def _pr_state_for_branch(repo: Path, branch: str) -> str | None:
    """Return MERGED / CLOSED / OPEN if a PR exists; None if no PR or gh missing."""

    if not branch:
        return None
    rc, out = _run(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "all",
            "--json",
            "state",
            "--limit",
            "1",
        ],
        cwd=repo,
        timeout=15,
    )
    if rc != 0:
        return None
    try:
        prs = json.loads(out or "[]")
    except json.JSONDecodeError:
        return None
    if not prs:
        return None
    return str(prs[0].get("state", "")).upper() or None


def _dirty_status(wt_path: Path) -> tuple[bool, bool]:
    """Return (is_dirty, dirty_is_only_nits).

    `is_dirty` reflects any modified, staged, or untracked content.
    `dirty_is_only_nits` is True iff every path in the dirt is in `_NIT_PATHS`.
    """

    if not wt_path.exists():
        return False, True
    rc, out = _run(["git", "status", "--porcelain"], cwd=wt_path)
    if rc != 0 or not out.strip():
        return False, True
    only_nits = True
    for line in out.splitlines():
        # porcelain format: "XY path" — path starts at column 3
        path = line[3:].strip()
        leaf = Path(path).name
        if leaf in _NIT_PATHS:
            continue
        # Also tolerate paths that are entirely under a nit dir
        if any(part in _NIT_PATHS for part in Path(path).parts):
            continue
        only_nits = False
        break
    return True, only_nits


def _default_branch(repo: Path) -> str:
    """Best-effort default branch name for `repo` (e.g. 'main')."""
    rc, out = _run(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=repo
    )
    if rc == 0 and out.strip():
        return out.strip().split("/", 1)[-1]  # "origin/main" -> "main"
    for cand in ("main", "master"):
        rc, _ = _run(["git", "rev-parse", "--verify", "--quiet", cand], cwd=repo)
        if rc == 0:
            return cand
    return "main"


def assess_staleness(
    wt: WorktreeInfo, repo: Path, default_branch: str | None = None
) -> StalenessVerdict:
    verdict = StalenessVerdict(worktree=wt)

    # S1 — directory gone
    if not wt.path.exists():
        verdict.is_stale = True
        verdict.reasons.append("S1: worktree directory missing on disk")
        verdict.can_force_prune = True
        return verdict

    # Default-branch guard — a linked worktree checked out on the default
    # branch is a base checkout, not stale feature work. Never flag it (its
    # HEAD would trivially trip S3 as an ancestor of main).
    if default_branch and wt.branch == default_branch:
        verdict.reasons.append(
            f"on default branch '{default_branch}' — base checkout, not stale"
        )
        return verdict

    if wt.prunable_per_git:
        verdict.is_stale = True
        verdict.reasons.append("S1: git marks worktree prunable")

    # Dirty floor — block force-prune if dirty with non-nit content
    is_dirty, only_nits = _dirty_status(wt.path)
    verdict.is_dirty = is_dirty
    if is_dirty and not only_nits:
        verdict.can_force_prune = False

    # S2 — branch deleted on origin
    if wt.branch and not _branch_exists_on_origin(repo, wt.branch):
        verdict.is_stale = True
        verdict.reasons.append(
            f"S2: branch '{wt.branch}' has been deleted on origin"
        )

    # S3 — head is ancestor of default branch
    if _is_ancestor_of_default(repo, wt.head_sha):
        verdict.is_stale = True
        verdict.reasons.append(
            "S3: worktree HEAD is already an ancestor of origin/main"
        )

    # S4 — PR merged or closed
    pr_state = _pr_state_for_branch(repo, wt.branch)
    if pr_state in ("MERGED", "CLOSED"):
        verdict.is_stale = True
        verdict.reasons.append(f"S4: PR for '{wt.branch}' is {pr_state}")

    # Locked worktrees with no other signals stay non-stale; TIDY respects locks.
    return verdict


def find_stale(repo: Path) -> list[StalenessVerdict]:
    """List worktrees of `repo` and assess each. Excludes the main worktree."""

    rc, main_path = _run(["git", "rev-parse", "--show-toplevel"], cwd=repo)
    main = Path(main_path.strip()) if rc == 0 else repo.resolve()

    default_branch = _default_branch(repo)
    verdicts: list[StalenessVerdict] = []
    for wt in list_worktrees(repo):
        if wt.path.resolve() == main.resolve():
            continue
        verdicts.append(assess_staleness(wt, repo, default_branch=default_branch))
    return verdicts


@dataclass
class PrunePlan:
    """Deterministic decision: which stale worktrees may be removed, and why not.

    This is the safety floor *under* TIDY's staleness/LLM judgment. The signals
    (and any model reasoning in `drift`/`diagnose`) decide what is *stale*; this
    decides what is *safe to remove without a human*. It never auto-removes a
    worktree another session may hold (locked) — only an explicit `--only` that
    names it counts as taking responsibility.
    """

    to_prune: list[StalenessVerdict] = field(default_factory=list)
    skipped: list[tuple[StalenessVerdict, str]] = field(default_factory=list)


def plan_prune(
    verdicts: list[StalenessVerdict],
    *,
    force: bool = False,
    only: list[str] | None = None,
    exclude: list[str] | None = None,
) -> PrunePlan:
    """Apply the safety floors to a set of staleness verdicts.

    - Only `is_stale` worktrees are candidates.
    - `exclude` drops named paths; `only` (when given) keeps *only* named paths.
    - **Locked** worktrees are skipped unless explicitly named in `only`
      (a blanket `--force` does NOT steal a lock — that's git's own rule).
    - **Dirty** (non-nit) worktrees are skipped unless `force` or named in `only`.
    """
    only_set = {Path(p).resolve() for p in only} if only is not None else None
    exclude_set = {Path(p).resolve() for p in (exclude or [])}
    plan = PrunePlan()
    for v in verdicts:
        if not v.is_stale:
            continue
        p = v.worktree.path.resolve()
        explicit = only_set is not None and p in only_set
        if p in exclude_set:
            plan.skipped.append((v, "excluded by --exclude"))
            continue
        if only_set is not None and not explicit:
            plan.skipped.append((v, "not selected by --only"))
            continue
        if v.worktree.locked and not explicit:
            plan.skipped.append(
                (v, "locked — a worktree/session may hold it; name it in --only to reclaim")
            )
            continue
        if v.is_dirty and not v.can_force_prune and not force and not explicit:
            plan.skipped.append(
                (v, "dirty (non-nit changes); pass --force or name it in --only")
            )
            continue
        plan.to_prune.append(v)
    return plan
