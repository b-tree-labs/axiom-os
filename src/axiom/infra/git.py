# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Git helpers — subprocess wrappers for common git operations.

Provides a thin layer over ``git`` subprocess calls with consistent
error handling. No external dependencies (no GitPython, no pygit2).

Usage:
    from axiom.infra.git import run_git, git_sha, git_branch, git_is_dirty

    sha = git_sha(repo)               # current HEAD SHA
    branch = git_branch(repo)          # current branch name
    dirty = git_is_dirty(repo)         # uncommitted changes?
    output = run_git(repo, "log", "--oneline", "-5")  # arbitrary command
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def safe_git_env(repo_root: Path | str | None = None) -> dict[str, str]:
    """Return an env dict safe for running ``git`` in a subprocess.

    Two protections, both required for correctness:

    1. **Strips every ``GIT_*`` env var inherited from the parent
       process.** Git hooks (notably ``pre-push``) and worktree
       contexts propagate ``GIT_DIR``, ``GIT_WORK_TREE``,
       ``GIT_INDEX_FILE``, ``GIT_OBJECT_DIRECTORY``, etc. into
       subprocesses. ``GIT_DIR`` in particular short-circuits *all*
       git repo discovery — it overrides ``cwd=``, ``-C <path>``, and
       ``GIT_CEILING_DIRECTORIES``. Without stripping, a subprocess
       silently operates on whatever repo the parent's GIT_DIR points
       at (typically the host workspace).

    2. **Sets ``GIT_CEILING_DIRECTORIES``** so git cannot ``chdir``
       upward past ``repo_root`` while searching for ``.git/``. If no
       ``.git/`` is present at ``repo_root`` or below, git returns
       its normal "not a git repository" error rather than walking up
       to a parent repo.

    Args:
        repo_root: Anchor for the ceiling. When ``None`` (default), no
            ``GIT_CEILING_DIRECTORIES`` is set — git uses normal
            discovery from the subprocess's ``cwd``. Pass a path when
            you want the additional defense-in-depth guarantee that
            git cannot walk above that path.

    Returns:
        A new env dict (modify-safe; not aliased to ``os.environ``)
        with stripped ``GIT_*`` keys plus, if ``repo_root`` was given,
        ``GIT_CEILING_DIRECTORIES`` set to its resolved path. Callers
        that want config-isolation set ``GIT_CONFIG_GLOBAL`` and
        ``GIT_CONFIG_SYSTEM`` separately via the canonical
        ``axiom.extensions.builtins.hygiene._git_isolation.git_isolated_env``.
    """
    env = os.environ.copy()
    for k in list(env):
        if k.startswith("GIT_"):
            del env[k]
    # Never hang on an interactive credential/username prompt: automation
    # has no terminal to answer it (stdin is typically /dev/null), so a
    # prompt becomes an indefinite hang. Fail fast instead. Set after the
    # GIT_* strip above so it survives. (Caught the 2026-05-26 case where
    # `git push origin --delete` blocked on osxkeychain.)
    env["GIT_TERMINAL_PROMPT"] = "0"
    if repo_root is not None:
        env["GIT_CEILING_DIRECTORIES"] = str(Path(repo_root).resolve())
    return env


def run_git(repo_root: Path, *args: str, check: bool = True) -> str:
    """Run a git command and return stdout, scoped strictly to ``repo_root``.

    The subprocess env is built via :func:`safe_git_env` — see that
    function's docstring for the strip-``GIT_*`` + ceiling rationale.
    The history: callers that pass a path lacking a ``.git`` (e.g. a
    ``tmp_path`` in tests, or a project directory under a larger
    workspace repo) used to silently get state from a parent repository
    — the same vector that produced the 2026-05-04 / 2026-05-11
    tester-pollution incidents.

    Args:
        repo_root: Working directory for the git command. The ceiling
            constraint pins git to find ``.git`` at this path or below
            only; if no ``.git`` is present, git returns its normal
            "not a git repository" error rather than walking up.
        *args: Arguments to ``git`` (e.g., ``"rev-parse"``, ``"HEAD"``).
        check: If True (default), raise on non-zero exit.

    Returns:
        Stripped stdout from the git command.

    Raises:
        subprocess.CalledProcessError: If check=True and git exits non-zero.
        FileNotFoundError: If git is not installed.
    """
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=check,
        env=safe_git_env(repo_root),
    )
    return result.stdout


def git_sha(repo_root: Path, *, short: bool = False) -> str:
    """Current HEAD commit SHA."""
    args = ["rev-parse"]
    if short:
        args.append("--short")
    args.append("HEAD")
    return run_git(repo_root, *args).strip()


def git_branch(repo_root: Path) -> str:
    """Current branch name (or 'HEAD' if detached)."""
    return run_git(repo_root, "rev-parse", "--abbrev-ref", "HEAD").strip()


def git_is_dirty(repo_root: Path) -> bool:
    """True if the working tree has uncommitted changes."""
    return bool(run_git(repo_root, "status", "--porcelain").strip())


def git_remote_url(repo_root: Path, remote: str = "origin") -> str | None:
    """Remote URL, or None if no remote configured."""
    try:
        return run_git(repo_root, "remote", "get-url", remote).strip()
    except subprocess.CalledProcessError:
        return None


def git_diff_files(
    repo_root: Path, since: str, path_filter: str = ""
) -> list[str]:
    """Files changed since a commit, optionally filtered by path prefix."""
    args = ["diff", "--name-only", since]
    if path_filter:
        args.extend(["--", path_filter])
    try:
        output = run_git(repo_root, *args)
        return [line.strip() for line in output.strip().splitlines() if line.strip()]
    except subprocess.CalledProcessError:
        return []


def git_available() -> bool:
    """True if the ``git`` binary is on PATH."""
    return shutil.which("git") is not None


def is_git_repo(path: Path | str) -> bool:
    """True if ``path`` is a git repo root (has its own ``.git``).

    A pure filesystem check: a ``.git`` directory *or* file (linked
    worktrees and submodules use a file) directly at ``path``. A ``.git``
    in a *parent* directory does not count — use :func:`is_inside_work_tree`
    for the walk-up question. Works even when the git binary is absent.
    """
    return (Path(path) / ".git").exists()


def is_inside_work_tree(path: Path | str) -> bool:
    """True if ``path`` is inside any git work tree (walks up to a parent).

    Unlike :func:`is_git_repo`, this uses normal git discovery with no
    ceiling, so a subdirectory of a repo reports True. Use this to decide
    whether offering ``git init`` would create a nested repo.
    """
    if not git_available():
        return False
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(path),
            capture_output=True,
            text=True,
            check=False,
            env=safe_git_env(None),  # no ceiling → normal upward discovery
        )
    except FileNotFoundError:
        return False
    return result.stdout.strip() == "true"


def init_repo(path: Path | str, *, initial_branch: str = "main") -> None:
    """Initialize a git repo at ``path`` (idempotent; no-op if already one).

    Falls back to a plain ``git init`` on git versions predating ``-b``
    (< 2.28), so it works across the varied hosts an agent may run on.
    """
    p = Path(path)
    if is_git_repo(p):
        return
    p.mkdir(parents=True, exist_ok=True)
    try:
        run_git(p, "init", "-b", initial_branch)
    except subprocess.CalledProcessError:
        run_git(p, "init")  # older git without -b support


__all__ = [
    "safe_git_env",
    "run_git",
    "git_sha",
    "git_branch",
    "git_is_dirty",
    "git_remote_url",
    "git_diff_files",
    "git_available",
    "is_git_repo",
    "is_inside_work_tree",
    "init_repo",
]
