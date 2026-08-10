# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Core sync/check/init for the cross-provider project-context capability.

Three operations over a repo whose canonical context lives in ``AGENTS.md``:

  * :func:`sync`  — (re)generate the per-tool files (idempotent).
  * :func:`check` — report drift without writing (the CI / pre-commit gate).
  * :func:`init`  — scaffold ``AGENTS.md`` + the ``CLAUDE.md`` symlink, run the
    first sync, and install the pre-commit hook (the adoption path, ADR-051 §D).

All paths are repo-relative. Nothing here reaches outside ``root`` except the
pre-commit hook, which writes into the repo's own ``.git/hooks``.
"""

from __future__ import annotations

import stat
from dataclasses import asdict, dataclass
from pathlib import Path

from .generators import TARGETS

__all__ = [
    "CANONICAL",
    "TargetResult",
    "InitResult",
    "Finding",
    "canonical_path",
    "read_canonical",
    "repo_root_default",
    "sync",
    "check",
    "has_drift",
    "findings",
    "init",
    "install_precommit_hook",
    "STARTER_AGENTS",
]

CANONICAL = "AGENTS.md"

# Statuses a target can carry. sync: created|written|unchanged. check: ok|drift|missing.
_DRIFT_STATUSES = frozenset({"drift", "missing"})


@dataclass
class TargetResult:
    target: str
    path: str
    status: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class InitResult:
    created_canonical: bool
    promoted_from_claude: bool
    symlinked_claude: bool
    sync_results: list[TargetResult]
    hook: str  # installed | unchanged | no_git

    def to_dict(self) -> dict:
        return {
            "created_canonical": self.created_canonical,
            "promoted_from_claude": self.promoted_from_claude,
            "symlinked_claude": self.symlinked_claude,
            "sync_results": [r.to_dict() for r in self.sync_results],
            "hook": self.hook,
        }


def canonical_path(root: Path | str) -> Path:
    return Path(root) / CANONICAL


def read_canonical(root: Path | str) -> str | None:
    p = canonical_path(root)
    return p.read_text(encoding="utf-8") if p.exists() else None


def repo_root_default() -> Path:
    """Best-effort repo root: git toplevel, else the current directory."""
    try:
        from axiom.infra.git import git_available, run_git

        if git_available():
            top = run_git(
                Path.cwd(), "rev-parse", "--show-toplevel", check=False
            ).strip()
            if top:
                return Path(top)
    except Exception:
        pass
    return Path.cwd()


def sync(root: Path | str, *, write: bool = True) -> list[TargetResult]:
    """(Re)generate every target from ``AGENTS.md``.

    Returns one :class:`TargetResult` per target with status ``created`` /
    ``written`` / ``unchanged`` (the create/write status reflects what *would*
    happen when ``write`` is False — a dry-run preview). Returns ``[]`` when no
    canonical ``AGENTS.md`` exists; the caller decides how to surface that.
    """
    root = Path(root)
    agents = read_canonical(root)
    if agents is None:
        return []
    results: list[TargetResult] = []
    for t in TARGETS:
        rendered = t.render(agents)
        dest = root / t.relpath
        existed = dest.exists()
        current = dest.read_text(encoding="utf-8") if existed else None
        if current == rendered:
            status = "unchanged"
        else:
            status = "written" if existed else "created"
            if write:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(rendered, encoding="utf-8")
        results.append(TargetResult(t.name, t.relpath, status))
    return results


def check(root: Path | str) -> list[TargetResult]:
    """Report drift without writing: status ``ok`` / ``drift`` / ``missing``.

    Returns ``[]`` when no canonical ``AGENTS.md`` exists.
    """
    root = Path(root)
    agents = read_canonical(root)
    if agents is None:
        return []
    results: list[TargetResult] = []
    for t in TARGETS:
        dest = root / t.relpath
        rendered = t.render(agents)
        if not dest.exists():
            status = "missing"
        elif dest.read_text(encoding="utf-8") == rendered:
            status = "ok"
        else:
            status = "drift"
        results.append(TargetResult(t.name, t.relpath, status))
    return results


def has_drift(results: list[TargetResult]) -> bool:
    """True if any target is missing or drifted (the gate predicate)."""
    return any(r.status in _DRIFT_STATUSES for r in results)


@dataclass
class Finding:
    """A discovered 'this repo could upgrade' signal that carries its own fix.

    Shaped for ADR-051 §E: a `doctor` / TIDY heartbeat consumer calls
    :func:`findings`, surfaces these to the operator, and the embedded
    ``remediation`` is the exact one-liner to run.
    """

    code: str  # context.uninitialized | context.missing | context.drift
    severity: str  # "info" | "warn"
    message: str
    remediation: str

    def to_dict(self) -> dict:
        return asdict(self)


def findings(root: Path | str) -> list[Finding]:
    """Discovery: what about this repo's context setup needs attention.

    Read-only. Each finding names a concrete remediation command. Returns an
    empty list when the repo is fully in sync (nothing to surface).
    """
    root = Path(root)
    if read_canonical(root) is None:
        return [
            Finding(
                "context.uninitialized",
                "info",
                f"No AGENTS.md at {root} — cross-provider context not set up.",
                "run `axi context init` to adopt it",
            )
        ]
    results = check(root)
    out: list[Finding] = []
    missing = [r.target for r in results if r.status == "missing"]
    drifted = [r.target for r in results if r.status == "drift"]
    if missing:
        out.append(
            Finding(
                "context.missing",
                "warn",
                f"generated context file(s) missing: {', '.join(missing)}",
                "run `axi context sync`",
            )
        )
    if drifted:
        out.append(
            Finding(
                "context.drift",
                "warn",
                f"context file(s) drifted from AGENTS.md: {', '.join(drifted)}",
                "run `axi context sync`",
            )
        )
    return out


STARTER_AGENTS = """\
# Project context

This is the single canonical context file for AI coding assistants. Edit it,
then run `axi context sync` — the per-tool files (Cursor, JetBrains Junie,
Copilot) are generated from this one and must not be hand-edited.

## What this project is

<one paragraph: what the project does and who it is for>

## Conventions

- <coding conventions, naming, test discipline>

## Where things go

- <map of the repo: where new code / docs / tests belong>
"""


def init(root: Path | str, *, force: bool = False) -> InitResult:
    """Adopt the capability in ``root``. Idempotent.

    When ``AGENTS.md`` is absent but a **real** (non-symlink) ``CLAUDE.md``
    exists, that hand-authored file is *promoted* to canonical ``AGENTS.md``
    (moved, then a ``CLAUDE.md`` symlink points back) rather than overwritten
    with a starter — so a repo that already documented itself in ``CLAUDE.md``
    keeps its content. Otherwise a starter ``AGENTS.md`` is scaffolded when
    absent (or ``force``). Then the ``CLAUDE.md → AGENTS.md`` symlink is
    ensured, the first sync runs, and the pre-commit drift hook is installed.
    """
    root = Path(root)
    cp = canonical_path(root)
    claude = root / "CLAUDE.md"
    created = False
    promoted = False

    if not cp.exists() and not force and claude.is_file() and not claude.is_symlink():
        # Promote an existing hand-authored CLAUDE.md to canonical AGENTS.md.
        claude.replace(cp)
        promoted = True
    elif force or not cp.exists():
        cp.write_text(STARTER_AGENTS, encoding="utf-8")
        created = True

    symlinked = False
    if not claude.exists() and not claude.is_symlink():
        try:
            claude.symlink_to(CANONICAL)  # relative target → portable within repo
            symlinked = True
        except (OSError, NotImplementedError):
            # Windows without privilege, or a filesystem without symlinks:
            # AGENTS.md still works for Codex/Cursor; CLAUDE.md is best-effort.
            symlinked = False

    sync_results = sync(root, write=True)
    hook = install_precommit_hook(root)
    return InitResult(created, promoted, symlinked, sync_results, hook)


_HOOK_BEGIN = "# >>> axi context (managed) >>>"
_HOOK_END = "# <<< axi context (managed) <<<"
_HOOK_BLOCK = f"""{_HOOK_BEGIN}
# Keep generated assistant-context files in sync with AGENTS.md.
axi context check || {{
  echo 'context files drifted from AGENTS.md — run: axi context sync' >&2
  exit 1
}}
{_HOOK_END}
"""


def install_precommit_hook(root: Path | str) -> str:
    """Install (idempotently) a managed pre-commit block that runs the drift check.

    Returns ``"installed"``, ``"unchanged"`` (block already present), or
    ``"no_git"`` (no ``.git`` directory — e.g. not a primary checkout).
    """
    root = Path(root)
    git_dir = root / ".git"
    # Only handle the common primary-checkout case (a `.git` directory). In a
    # linked worktree `.git` is a file and hooks live in the common dir; skip
    # rather than guess.
    if not git_dir.is_dir():
        return "no_git"
    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-commit"
    if hook.exists():
        text = hook.read_text(encoding="utf-8")
        if _HOOK_BEGIN in text:
            return "unchanged"
        hook.write_text(text.rstrip() + "\n\n" + _HOOK_BLOCK, encoding="utf-8")
    else:
        hook.write_text("#!/bin/sh\n" + _HOOK_BLOCK, encoding="utf-8")
    mode = hook.stat().st_mode
    hook.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return "installed"
