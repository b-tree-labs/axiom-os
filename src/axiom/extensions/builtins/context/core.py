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

from .generators import GENERATED_MARKER, TARGETS


def _brand_cli() -> str:
    """The command the operator actually typed.

    These lines said "axi" unconditionally, so a consumer distribution's CLI
    told the operator to run a command that does not exist on their machine.
    """
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


def _brand_cli() -> str:
    """The command the operator actually typed.

    These lines said "axi" unconditionally, so a consumer distribution's CLI
    told the operator to run a command that does not exist on their machine.
    """
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"

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
CLAUDE = "CLAUDE.md"

# Statuses a target can carry. sync: created|written|unchanged|conflict.
# check: ok|drift|missing|conflict. `conflict` means a hand-authored file
# (no generated marker, or a diverged real CLAUDE.md) sits at a managed
# path — it is never overwritten; the human merges it into AGENTS.md.
_DRIFT_STATUSES = frozenset({"drift", "missing", "conflict"})


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


def _claude_status(root: Path) -> str:
    """How CLAUDE.md relates to canonical AGENTS.md.

    ``ok``: a symlink resolving to AGENTS.md, or a regular file whose content
    equals the canonical text (the no-symlink-support fallback), or a
    reversed-convention repo (AGENTS.md itself is the symlink) — managing
    CLAUDE.md there would create a symlink cycle, so it is out of scope.
    ``missing`` / ``drift`` (wrong symlink target) / ``conflict`` (a real,
    diverged CLAUDE.md — hand-authored content that must not be clobbered).
    """
    if (root / CANONICAL).is_symlink():
        return "ok"
    claude = root / CLAUDE
    if not claude.exists() and not claude.is_symlink():
        return "missing"
    if claude.is_symlink():
        try:
            same = claude.resolve() == (root / CANONICAL).resolve()
        except OSError:
            same = False
        return "ok" if same else "drift"
    agents = read_canonical(root)
    return "ok" if claude.read_text(encoding="utf-8") == agents else "conflict"


def _sync_claude(root: Path, *, write: bool) -> TargetResult:
    """Ensure ``CLAUDE.md`` is a symlink to ``AGENTS.md`` (see ADR-051).

    A diverged real CLAUDE.md is a ``conflict`` and is never touched. An
    identical real CLAUDE.md is upgraded to a symlink where the platform
    allows; where symlinks are unavailable the identical copy already counts
    as ``ok`` and is left in place.
    """
    status = _claude_status(root)
    claude = root / CLAUDE
    if status == "conflict" or (root / CANONICAL).is_symlink():
        return TargetResult("claude", CLAUDE, "conflict" if status == "conflict" else "unchanged")
    upgrade = status == "ok" and claude.exists() and not claude.is_symlink()
    if status == "ok" and not upgrade:
        return TargetResult("claude", CLAUDE, "unchanged")
    out = "created" if status == "missing" else "written"
    if write:
        try:
            if claude.exists() or claude.is_symlink():
                claude.unlink()
            claude.symlink_to(CANONICAL)  # relative target → portable within repo
        except (OSError, NotImplementedError):
            if status == "ok":
                return TargetResult("claude", CLAUDE, "unchanged")  # keep the copy
            claude.write_text(read_canonical(root) or "", encoding="utf-8")
    return TargetResult("claude", CLAUDE, out)


def sync(root: Path | str, *, write: bool = True) -> list[TargetResult]:
    """(Re)generate every target from ``AGENTS.md`` and manage the
    ``CLAUDE.md`` symlink.

    Returns one :class:`TargetResult` per target with status ``created`` /
    ``written`` / ``unchanged`` / ``conflict`` (the create/write status
    reflects what *would* happen when ``write`` is False — a dry-run
    preview). A destination holding hand-authored content — no generated
    marker and different from the rendering — is a ``conflict`` and is left
    untouched. Returns ``[]`` when no canonical ``AGENTS.md`` exists; the
    caller decides how to surface that.
    """
    root = Path(root)
    agents = read_canonical(root)
    if agents is None:
        return []
    results: list[TargetResult] = [_sync_claude(root, write=write)]
    for t in TARGETS:
        rendered = t.render(agents)
        dest = root / t.relpath
        existed = dest.exists()
        current = dest.read_text(encoding="utf-8") if existed else None
        if current == rendered:
            status = "unchanged"
        elif existed and GENERATED_MARKER not in (current or ""):
            status = "conflict"  # hand-authored file at a managed path
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
    results: list[TargetResult] = [TargetResult("claude", CLAUDE, _claude_status(root))]
    for t in TARGETS:
        dest = root / t.relpath
        rendered = t.render(agents)
        if not dest.exists():
            status = "missing"
        elif dest.read_text(encoding="utf-8") == rendered:
            status = "ok"
        elif GENERATED_MARKER not in dest.read_text(encoding="utf-8"):
            status = "conflict"
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
                f"run `{_brand_cli()} context init` to adopt it",
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
                f"run `{_brand_cli()} context sync`",
            )
        )
    if drifted:
        out.append(
            Finding(
                "context.drift",
                "warn",
                f"context file(s) drifted from AGENTS.md: {', '.join(drifted)}",
                f"run `{_brand_cli()} context sync`",
            )
        )
    conflicted = [r.target for r in results if r.status == "conflict"]
    if conflicted:
        out.append(
            Finding(
                "context.conflict",
                "warn",
                "hand-authored content at managed path(s): "
                + ", ".join(conflicted)
                + " — sync will not overwrite it",
                f"merge the content into AGENTS.md, delete the file, rerun `{_brand_cli()} context sync`",
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
