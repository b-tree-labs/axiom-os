# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Repo-wide git-state hygiene signals (issue #201).

Each signal is a pure function over the repo's file-tree + git state.
Returns `Finding` objects suitable for surfacing through
``axiom_signals__brief`` / ``node_health`` aggregation.

The signals here complement (not duplicate) :mod:`drift` (per-worktree
analysis) and :mod:`worktrees.find_stale` (worktree-staleness
verdicts). This module's surface is **repo-wide refs + reflog state**.
"""

from __future__ import annotations

import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from .node_health import Finding, Severity
from .worktrees import _is_ancestor_of_default, find_stale, list_worktrees

DEFAULT_STASH_DORMANCY_DAYS: int = 60
DEFAULT_SCAFFOLD_DORMANCY_DAYS: int = 14

# Filenames that legitimately appear at many tracked paths — don't flag
# them as duplicate-basename smells. Mostly Python package markers,
# README conventions, and test-dir conventions.
COMMON_BASENAMES: frozenset[str] = frozenset(
    {
        "__init__.py",
        "__main__.py",
        "conftest.py",
        "README.md",
        "README",
        "CHANGELOG.md",
        "Makefile",
        ".gitignore",
        ".gitkeep",
        ".keep",
        "manifest.json",
        "package.json",
        "tsconfig.json",
        "tests",
        "test",
        "docs",
        "fixtures",
    }
)

# Branches we never flag as stale. Conservative — these are typically
# project default / long-running integration branches.
PROTECTED_BRANCHES: frozenset[str] = frozenset(
    {"main", "master", "develop", "trunk"}
)


def _run(args: list[str], cwd: Path, timeout: int = 10) -> tuple[int, str]:
    """Run a subprocess with `GIT_*` env vars stripped from the parent.

    Critical for correctness under git hooks (notably pre-push): git
    sets `GIT_DIR`, `GIT_INDEX_FILE`, `GIT_WORK_TREE`, etc. before
    invoking hooks. Those env vars propagate into pytest and from
    pytest into every `subprocess.run(["git", ...])` call — short-
    circuiting `cwd=`/`-C` git-repo-discovery so the subprocess
    silently operates on the host repo instead of the target.

    `axiom.infra.git.safe_git_env(cwd)` is the canonical helper:
    strips every `GIT_*` from the env + sets `GIT_CEILING_DIRECTORIES`
    so git can't walk upward past `cwd` while searching for `.git/`.
    Matches the protection used by `_git_isolation.git_isolated_env`
    in sibling test fixtures.
    """
    from axiom.infra.git import safe_git_env

    try:
        result = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            check=False, env=safe_git_env(cwd),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 1, ""
    return result.returncode, result.stdout


def _current_branch(repo: Path) -> str:
    rc, out = _run(["git", "symbolic-ref", "--short", "-q", "HEAD"], cwd=repo)
    return out.strip() if rc == 0 else ""


def _local_branches(repo: Path) -> list[tuple[str, str]]:
    """Yield ``(branch_name, tip_sha)`` for every local ref under
    ``refs/heads/``."""
    rc, out = _run(
        [
            "git", "for-each-ref",
            "--format=%(refname:short) %(objectname)",
            "refs/heads/",
        ],
        cwd=repo,
    )
    if rc != 0:
        return []
    pairs: list[tuple[str, str]] = []
    for line in out.splitlines():
        parts = line.strip().split()
        if len(parts) == 2:
            pairs.append((parts[0], parts[1]))
    return pairs


def check_stale_branches(repo: Path) -> list[Finding]:
    """Local branches whose tip is reachable from ``origin/main`` and
    which are NOT currently checked out anywhere.

    A branch in this state is merged-by-content into the default
    branch with no remaining commits to land. Safe to delete; we mark
    the finding ``auto_fixable=True`` so TIDY can prune in batch.

    The current branch and any worktree-occupied branches are skipped:
    deleting what the user is on (or pruning a branch with an active
    worktree) would surprise the user.
    """
    in_use: set[str] = {wt.branch for wt in list_worktrees(repo) if wt.branch}
    current = _current_branch(repo)
    if current:
        in_use.add(current)

    findings: list[Finding] = []
    for branch, sha in _local_branches(repo):
        if branch in PROTECTED_BRANCHES:
            continue
        if branch in in_use:
            continue
        if not _is_ancestor_of_default(repo, sha):
            continue
        findings.append(
            Finding(
                check="stale_branch",
                severity=Severity.INFO,
                message=(
                    f"local branch {branch!r} is merged into origin/main; "
                    "safe to delete"
                ),
                current_value=branch,
                expected_value="(delete)",
                auto_fixable=True,
            )
        )
    return findings


def _branch_has_upstream(repo: Path, branch: str) -> bool:
    """True iff ``branch`` has a configured upstream (tracking branch).

    A branch that never had an upstream is local-only WIP, not a
    "deleted upstream" — telling them apart is the only reliable way
    to filter the S2 false-positive from :mod:`worktrees`."""
    if not branch:
        return False
    rc, _ = _run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name",
         f"{branch}@{{upstream}}"],
        cwd=repo,
    )
    return rc == 0


def _tracked_files(repo: Path) -> list[str]:
    rc, out = _run(["git", "ls-files"], cwd=repo, timeout=30)
    if rc != 0:
        return []
    return [line for line in out.splitlines() if line]


def check_dup_basenames(repo: Path) -> list[Finding]:
    """Same filename tracked at multiple paths — usually a leftover
    from a refactor (the 2026-05 cleanup found `scripts/setup-<host>.sh`
    AND `infra/scripts/setup-<host>.sh` coexisting).

    Filters ``COMMON_BASENAMES`` (``__init__.py``, ``README.md``, etc.)
    that legitimately recur — flagging those would just be noise.
    """
    groups: dict[str, list[str]] = {}
    for path in _tracked_files(repo):
        basename = path.rsplit("/", 1)[-1]
        if basename in COMMON_BASENAMES:
            continue
        groups.setdefault(basename, []).append(path)

    findings: list[Finding] = []
    for basename, paths in groups.items():
        if len(paths) < 2:
            continue
        paths_sorted = sorted(paths)
        findings.append(
            Finding(
                check="duplicate_basename",
                severity=Severity.INFO,
                message=(
                    f"{basename!r} tracked at {len(paths)} paths: "
                    + ", ".join(paths_sorted)
                ),
                current_value=" | ".join(paths_sorted),
                expected_value="(consolidate or rename)",
                auto_fixable=False,
            )
        )
    return findings


# Patterns that indicate a script has hardcoded developer-machine paths.
# Compiled once at module import; reused per-file.
_HARDCODED_PATH_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\$HOME/Projects/"),
    re.compile(r"/Users/[^/\s\"']+/Projects/"),
    re.compile(r"/home/[^/\s\"']+/Projects/"),
)


def check_scripts_with_hardcoded_paths(repo: Path) -> list[Finding]:
    """`scripts/*.sh` containing absolute developer paths can only run on
    one specific machine. A real ``scripts/<host>-heartbeat.sh`` was the
    motivating case — hardcoded ``$HOME/Projects/UT_Computational_NE/...``.

    Scope: only ``scripts/*.sh``. That's the documented portable-scripts
    namespace; other ``.sh`` paths (e.g. ``docs/example.sh``,
    deployment manifests) are intentionally machine-specific.

    Replacement is structural (substitute ``${REPO_DIR}`` /
    ``${PROJECT_ROOT}`` / ``${HOST}``) so findings are not
    ``auto_fixable``.
    """
    scripts_dir = repo / "scripts"
    if not scripts_dir.is_dir():
        return []
    findings: list[Finding] = []
    for script in sorted(scripts_dir.glob("*.sh")):
        try:
            content = script.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        matches: list[str] = []
        for pat in _HARDCODED_PATH_PATTERNS:
            m = pat.search(content)
            if m:
                matches.append(m.group(0))
        if not matches:
            continue
        sample = matches[0]
        rel = script.relative_to(repo)
        findings.append(
            Finding(
                check="hardcoded_developer_path",
                severity=Severity.WARNING,
                message=(
                    f"{rel} contains hardcoded developer path "
                    f"{sample!r}; can't run on other hosts. "
                    "Replace with ${REPO_DIR} or ${PROJECT_ROOT}."
                ),
                current_value=str(rel),
                expected_value="(parameterize developer-machine paths)",
                auto_fixable=False,
            )
        )
    return findings


def check_self_similar_dirs(repo: Path) -> list[Finding]:
    """Tracked paths containing an ``X/X/`` pattern — almost always a
    path-duplication accident (the 2026-05 `infra/infra/` was the
    motivating example).
    """
    seen: set[str] = set()
    for path in _tracked_files(repo):
        parts = path.split("/")
        for i in range(len(parts) - 1):
            if parts[i] and parts[i] == parts[i + 1]:
                marker = "/".join(parts[: i + 2])
                seen.add(marker)
                break
    findings: list[Finding] = []
    for marker in sorted(seen):
        findings.append(
            Finding(
                check="self_similar_directory",
                severity=Severity.WARNING,
                message=(
                    f"path contains a self-similar directory tree at "
                    f"{marker!r} — likely a path-duplication accident"
                ),
                current_value=marker,
                expected_value="(audit and remove duplicate level)",
                auto_fixable=False,
            )
        )
    return findings


def check_non_graduated_scaffolds(
    repo: Path,
    *,
    dormancy_days: int = DEFAULT_SCAFFOLD_DORMANCY_DAYS,
) -> list[Finding]:
    """Scaffolds in `<project_root>/.axi/scaffold-graduation.json` that
    have not yet been marked graduated AND are older than
    ``dormancy_days`` surface as Findings.

    Default 14 days — long enough that a working iteration cycle isn't
    flagged (most scaffolds graduate within a week or two of `axi ext
    init`), short enough that abandoned prototypes don't sit untouched
    for the user's typical "I'll come back to it" timescale.

    Graduation is a structural decision (does the extension have a
    first non-trivial test? does lint pass?), not a deletion-style
    auto-fix, so findings are ``auto_fixable=False``.
    """
    from axiom.cli.ext.scaffold_registry import list_non_graduated

    threshold = datetime.now(UTC).timestamp() - dormancy_days * 86400.0
    findings: list[Finding] = []
    for rec in list_non_graduated(repo):
        try:
            created_ts = datetime.fromisoformat(rec.created_at).timestamp()
        except (ValueError, TypeError):
            continue
        if created_ts > threshold:
            continue
        age_days = int((datetime.now(UTC).timestamp() - created_ts) / 86400.0)
        findings.append(
            Finding(
                check="non_graduated_scaffold",
                severity=Severity.INFO,
                message=(
                    f"scaffold {rec.name!r} at {rec.path!r} is "
                    f"{age_days}d old and not yet graduated"
                ),
                current_value=rec.name,
                expected_value=(
                    "(graduate via `axi ext graduate` once the extension "
                    "passes lint + has a first non-trivial test, or remove "
                    "the scaffold)"
                ),
                auto_fixable=False,
            )
        )
    return findings


def check_dormant_stashes(
    repo: Path,
    *,
    dormancy_days: int = DEFAULT_STASH_DORMANCY_DAYS,
) -> list[Finding]:
    """Stashes older than ``dormancy_days`` (default 60) are surfaced
    as candidates for review or drop.

    Stash drop is destructive (lost work is unrecoverable without the
    reflog), so findings carry ``auto_fixable=False`` — TIDY surfaces
    them, the human decides.
    """
    # `git stash list` with a parsable timestamp + the human-readable ref
    rc, out = _run(
        ["git", "stash", "list", "--format=%gd %at %s"],
        cwd=repo,
    )
    if rc != 0 or not out.strip():
        return []
    threshold = datetime.now(UTC).timestamp() - dormancy_days * 86400.0
    findings: list[Finding] = []
    for line in out.splitlines():
        parts = line.split(" ", 2)
        if len(parts) < 3:
            continue
        ref, ts_str, subject = parts
        try:
            ts = float(ts_str)
        except ValueError:
            continue
        if ts > threshold:
            continue
        age_days = int((datetime.now(UTC).timestamp() - ts) / 86400.0)
        findings.append(
            Finding(
                check="dormant_stash",
                severity=Severity.INFO,
                message=(
                    f"{ref} is {age_days}d old: {subject!r}"
                ),
                current_value=ref,
                expected_value="(review or drop)",
                auto_fixable=False,
            )
        )
    return findings


def check_orphan_worktrees(repo: Path) -> list[Finding]:
    """Worktrees that git marks prunable, or whose branch is already on
    origin/main, or whose branch has been deleted on origin.

    Wraps :func:`worktrees.find_stale` — the verdict logic already
    encodes the S1/S2/S3/S4 staleness rules — and reshapes each
    verdict into a Finding keyed on the worktree's filesystem path so
    a human can act on it directly.

    Filters one false-positive: a local-only branch (no upstream
    configured) trips S2 ("deleted on origin") because
    ``git ls-remote`` can't distinguish "deleted" from "never pushed."
    If S2 is the *only* reason and the branch has no upstream, skip —
    that's an active WIP branch, not orphan.
    """
    findings: list[Finding] = []
    for verdict in find_stale(repo):
        if not verdict.is_stale:
            continue
        wt = verdict.worktree
        # False-positive filter: S2-only verdict on a never-pushed branch.
        only_s2 = (
            len(verdict.reasons) == 1
            and verdict.reasons[0].startswith("S2:")
        )
        if only_s2 and not _branch_has_upstream(repo, wt.branch):
            continue
        reason = "; ".join(verdict.reasons) if verdict.reasons else "stale"
        findings.append(
            Finding(
                check="orphan_worktree",
                severity=Severity.INFO,
                message=(
                    f"worktree at {wt.path} ({wt.branch or 'detached'}) "
                    f"is stale: {reason}"
                ),
                current_value=str(wt.path),
                expected_value="(remove)",
                auto_fixable=verdict.can_force_prune,
            )
        )
    return findings
