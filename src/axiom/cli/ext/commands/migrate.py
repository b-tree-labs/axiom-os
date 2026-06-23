# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi ext migrate`` — layout + version migrations for AEOS extensions.

Two mutually-exclusive modes:

1. ``--to-aeos-layout`` (default when no version flags given and the layout
   is pre-AEOS): move a flat legacy layout (``foo_agent/`` with root-level
   ``contract.py``, ``chat_tools.py``, etc.) to the canonical compound
   shape per ``docs/working/prompt-extension-migration.md``. Supported
   mappings are limited — unknown files are copied to the new root and
   listed in ``MIGRATION.md``. Unexpected shapes are surfaced loudly.

2. ``--from-version X --to-version Y``: AEOS spec-version upgrade. For
   AEOS 0.1 the only version is ``0.1.0``, so same-version is a no-op
   success and every other combo exits non-zero. The flag plumbing exists
   so AEOS 0.2 migrations drop in without reshaping the CLI.

Supported pre-AEOS shape (per migration doc):

    foo_agent/
      __init__.py          → foo/foo/__init__.py
      agent.py             → foo/foo/agents/agent.py
      reviewer.py          → foo/foo/agents/reviewer.py
      cli.py               → foo/foo/commands/cli.py
      chat_tools.py        → foo/foo/tools/chat_tools.py
      contract.py          → foo/foo/_internal/contract.py
      AGENT.md             → foo/docs/AGENT.md
      SKILLS.md            → foo/docs/SKILLS.md
      ROUTINES.md          → foo/docs/ROUTINES.md
      README.md            → foo/README.md
      axiom-extension.toml → foo/axiom-extension.toml (copied; fix manually)

Anything else is copied to ``foo/`` root and called out in MIGRATION.md.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from axiom.cli.ext._output import console
from axiom.cli.ext.provider import CliContext

# Only AEOS 0.1.0 exists today. Extending this set is how future migrations
# wire in — list the stepping-stone pairs and their upgrade function.
_SUPPORTED_VERSION_HOPS: dict[tuple[str, str], str] = {
    ("0.1.0", "0.1.0"): "no-op",
}


# Type suffixes the spec forbids. Migration strips a trailing type suffix
# to derive the AEOS purpose name (e.g. ``foo_agent`` → ``foo``).
_TYPE_SUFFIXES: tuple[str, ...] = (
    "_agent",
    "_tool",
    "_cmd",
    "_command",
    "_service",
    "_adapter",
    "_skill",
    "_hook",
)

# Explicit move table: filename → destination relative to the new package.
# Keys not in this table land at the package root and are called out in
# MIGRATION.md so the human can triage.
_SOURCE_MOVES: dict[str, str] = {
    "agent.py": "agents/agent.py",
    "reviewer.py": "agents/reviewer.py",
    "cli.py": "commands/cli.py",
    "chat_tools.py": "tools/chat_tools.py",
    "contract.py": "_internal/contract.py",
    "__init__.py": "__init__.py",
}

# Docs that move to the extension's docs/ tree rather than the package.
_DOC_MOVES: dict[str, str] = {
    "AGENT.md": "docs/AGENT.md",
    "SKILLS.md": "docs/SKILLS.md",
    "ROUTINES.md": "docs/ROUTINES.md",
}

# Files that move to the new extension root verbatim.
_ROOT_MOVES: dict[str, str] = {
    "README.md": "README.md",
    "CHANGELOG.md": "CHANGELOG.md",
    "LICENSE": "LICENSE",
    "pyproject.toml": "pyproject.toml",
    "axiom-extension.toml": "axiom-extension.toml",
}


def _purpose_name(raw: str) -> str:
    for suffix in _TYPE_SUFFIXES:
        if raw.endswith(suffix):
            return raw[: -len(suffix)]
    return raw


def _is_inside_git_worktree(path: Path) -> bool:
    from axiom.infra.git import safe_git_env
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
            env=safe_git_env(path),
        )
        return proc.returncode == 0 and proc.stdout.strip() == "true"
    except FileNotFoundError:  # pragma: no cover — git missing
        return False


def _move(src: Path, dst: Path, *, use_git: bool) -> None:
    """Move ``src`` to ``dst``, preferring ``git mv`` when possible."""
    from axiom.infra.git import safe_git_env
    dst.parent.mkdir(parents=True, exist_ok=True)
    if use_git:
        proc = subprocess.run(
            ["git", "mv", str(src), str(dst)],
            capture_output=True,
            text=True,
            check=False,
            env=safe_git_env(src.parent),
        )
        if proc.returncode == 0:
            return
        # Untracked files fail `git mv`; fall through to shutil.
    shutil.move(str(src), str(dst))


def _already_aeos_compound(ext_path: Path) -> bool:
    """A layout is 'already AEOS' if it has a purpose-named package dir."""
    if not (ext_path / "axiom-extension.toml").exists():
        return False
    # The scaffold emits <ext>/<ext>/__init__.py with capability subdirs.
    name = ext_path.name
    candidate = ext_path / name
    if not candidate.is_dir() or not (candidate / "__init__.py").exists():
        return False
    # The scaffold also emits at least one capability-kind subdirectory.
    return any((candidate / sub).is_dir() for sub in (
        "agents", "tools", "commands", "services", "adapters", "skills", "hooks",
    ))


# ---------------------------------------------------------------------------
# Plan preview
# ---------------------------------------------------------------------------


def plan_aeos_migration(
    legacy_path: Path, *, target_name: str | None = None
) -> dict[str, Any]:
    """Return a dry-run plan for :func:`migrate_to_aeos_layout` on ``legacy_path``.

    No disk writes. The returned dict mirrors what the real migration emits,
    but each ``moves`` entry is ``(src, dst)`` without actually having been
    performed yet.
    """
    if not legacy_path.is_dir():
        raise FileNotFoundError(f"migrate source does not exist: {legacy_path}")
    name = target_name or _purpose_name(legacy_path.name)
    new_root = legacy_path.parent / name

    moves: list[tuple[str, str]] = []
    other: list[str] = []
    if not legacy_path.exists():
        return {"moves": moves, "new_root": new_root, "other": other, "name": name}
    for child in sorted(legacy_path.iterdir()):
        n = child.name
        if n in _SOURCE_MOVES:
            dst = new_root / name / _SOURCE_MOVES[n]
        elif n in _DOC_MOVES:
            dst = new_root / _DOC_MOVES[n]
        elif n in _ROOT_MOVES:
            dst = new_root / _ROOT_MOVES[n]
        elif child.is_dir() and n in (
            "agents", "tools", "commands", "services", "adapters",
            "skills", "hooks", "_internal",
        ):
            dst = new_root / name / n
        elif child.is_dir() and n == "tests":
            dst = new_root / "tests"
        elif child.is_dir() and n == "docs":
            dst = new_root / "docs"
        elif n.startswith(".") or n == "__pycache__":
            continue
        else:
            dst = new_root / name / n
            other.append(n)
        moves.append((str(child), str(dst)))
    return {"moves": moves, "new_root": new_root, "other": other, "name": name}


# ---------------------------------------------------------------------------
# Layout migration
# ---------------------------------------------------------------------------


def migrate_to_aeos_layout(
    legacy_path: Path, *, target_name: str | None = None
) -> dict[str, Any]:
    """Move a pre-AEOS flat extension into the canonical compound layout.

    Args:
        legacy_path: Path to the legacy directory (e.g. ``foo_agent/``).
        target_name: Optional explicit AEOS name. Defaults to ``legacy_path.name``
            with its type suffix stripped.

    Returns:
        A summary dict with ``"moves"`` (list of ``(src, dst)``), ``"new_root"``
        (the new extension directory), and ``"other"`` (files that didn't
        match the explicit mapping and were relocated to the package root).
    """
    if not legacy_path.is_dir():
        raise FileNotFoundError(f"migrate source does not exist: {legacy_path}")

    name = target_name or _purpose_name(legacy_path.name)
    parent = legacy_path.parent
    new_root = parent / name
    # If the destination would collide (other than legacy == new_root),
    # refuse rather than trample the user's work.
    if new_root.exists() and new_root != legacy_path:
        raise FileExistsError(
            f"migrate target {new_root} already exists; move/delete it first"
        )

    # Snapshot the legacy children BEFORE we create anything inside it —
    # in-place migrations (legacy == new_root) would otherwise iterate over
    # our own freshly-created pkg_root/docs/ etc.
    legacy_children = sorted(legacy_path.iterdir())

    # Create the destination tree up front so moves can plant into it.
    # The intermediate directory may be the same as legacy when a purpose-
    # named legacy directory is being migrated in-place.
    new_root.mkdir(parents=True, exist_ok=True)
    pkg_root = new_root / name
    pkg_root.mkdir(parents=True, exist_ok=True)
    (new_root / "docs").mkdir(parents=True, exist_ok=True)

    use_git = _is_inside_git_worktree(legacy_path)

    moves: list[tuple[str, str]] = []
    other_files: list[str] = []

    for child in legacy_children:
        name_in = child.name

        if name_in in _SOURCE_MOVES:
            dst = pkg_root / _SOURCE_MOVES[name_in]
        elif name_in in _DOC_MOVES:
            dst = new_root / _DOC_MOVES[name_in]
        elif name_in in _ROOT_MOVES:
            dst = new_root / _ROOT_MOVES[name_in]
        elif child.is_dir() and name_in in (
            "agents", "tools", "commands", "services", "adapters",
            "skills", "hooks", "_internal",
        ):
            # Already typed subdirectory — move under the package root.
            dst = pkg_root / name_in
        elif child.is_dir() and name_in == "tests":
            dst = new_root / "tests"
        elif child.is_dir() and name_in == "docs":
            dst = new_root / "docs"
        elif name_in.startswith("."):
            # Hidden files (e.g. .gitkeep): skip to avoid clutter in the new tree.
            continue
        elif name_in == "__pycache__":
            continue
        else:
            # Unknown file — land in the package root and note it for the human.
            dst = pkg_root / name_in
            other_files.append(name_in)

        if dst == child:
            continue  # in-place (e.g. a same-named purpose dir)
        _move(child, dst, use_git=use_git)
        moves.append((str(child), str(dst)))

    # Remove the now-empty legacy dir when it's distinct from the new root.
    if legacy_path != new_root and legacy_path.exists():
        try:
            legacy_path.rmdir()
        except OSError:
            # Fall back to recursive removal for stubborn caches.
            shutil.rmtree(legacy_path, ignore_errors=True)

    _write_migration_md(new_root, name=name, moves=moves, other=other_files)

    return {"moves": moves, "new_root": new_root, "other": other_files}


def _write_migration_md(
    new_root: Path, *, name: str, moves: list[tuple[str, str]], other: list[str]
) -> None:
    lines = [
        f"# {name} — AEOS Migration Log",
        "",
        "This extension was migrated to the canonical AEOS 0.1 compound layout by",
        "`axi ext migrate --to-aeos-layout`. The table below records the move set.",
        "",
        "## File moves",
        "",
    ]
    for src, dst in moves:
        lines.append(f"- `{Path(src).name}` → `{Path(dst).relative_to(new_root)}`")
    if other:
        lines.append("")
        lines.append("## Unmapped files")
        lines.append("")
        lines.append(
            "The following files had no explicit AEOS mapping and were placed at "
            "the package root. Triage them manually."
        )
        lines.append("")
        for name_in in other:
            lines.append(f"- `{name_in}`")
    lines.append("")
    lines.append(
        "See `docs/working/prompt-extension-migration.md` for the follow-up checklist."
    )
    (new_root / "MIGRATION.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class MigrateProvider:
    """Built-in provider for ``axi ext migrate [<path>] [flags]``."""

    verb = "migrate"
    description = "Migrate an extension layout or AEOS spec version"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "path",
            nargs="?",
            default=None,
            help="Path to the extension (default: current working directory)",
        )
        parser.add_argument(
            "--to-aeos-layout",
            action="store_true",
            help="Migrate a pre-AEOS flat layout to the canonical compound layout",
        )
        parser.add_argument(
            "--from-version",
            default=None,
            help="Current AEOS spec version (for version-to-version upgrades)",
        )
        parser.add_argument(
            "--to-version",
            default=None,
            help="Target AEOS spec version (for version-to-version upgrades)",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Skip the confirmation prompt (e.g. in scripts or CI)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the migration plan without touching disk",
        )

    def run(self, args: argparse.Namespace, context: CliContext) -> int:
        target = Path(args.path).resolve() if args.path else context.cwd
        con = console()

        # Mode resolution: explicit version flags take the version path; else
        # layout migration.
        if args.from_version is not None or args.to_version is not None:
            if args.from_version is None or args.to_version is None:
                con.print(
                    "axi ext migrate: both --from-version and --to-version "
                    "must be supplied together"
                )
                return 2
            hop = (args.from_version, args.to_version)
            if hop not in _SUPPORTED_VERSION_HOPS:
                con.print(
                    f"axi ext migrate: no migration registered for "
                    f"{args.from_version} → {args.to_version}"
                )
                return 1
            if _SUPPORTED_VERSION_HOPS[hop] == "no-op":
                con.print(
                    f"axi ext migrate: no migration between {args.from_version} "
                    f"and {args.to_version} (AEOS spec versions are identical)"
                )
                return 0
            # Future spec versions plug in their handler here.
            con.print("axi ext migrate: version handler not implemented")
            return 1

        # Layout mode — implicit default when no flags are passed and the
        # layout is pre-AEOS.
        if _already_aeos_compound(target):
            con.print(
                f"axi ext migrate: {target} already matches the AEOS compound "
                "layout; nothing to do"
            )
            return 0

        # Auto-detect pre-AEOS layout: surface the plan, then either execute
        # (with --yes / TTY confirmation) or print the plan and exit 2 when
        # stdin is a pipe (tests, CI) without an explicit flag.
        try:
            plan = plan_aeos_migration(target)
        except FileNotFoundError as exc:
            con.print(f"axi ext migrate: {exc}")
            return 1
        except Exception as exc:  # noqa: BLE001
            con.print(f"axi ext migrate: failed to build plan — {exc}")
            return 1

        con.print(f"axi ext migrate: detected pre-AEOS layout at {target}")
        con.print(f"  proposed new root: {plan['new_root']}")
        con.print(f"  proposed AEOS name: {plan['name']}")
        con.print(f"  {len(plan['moves'])} file(s) to move:")
        for src, dst in plan["moves"][:12]:
            con.print(f"    {Path(src).name} -> {Path(dst)}")
        if len(plan["moves"]) > 12:
            con.print(f"    ... and {len(plan['moves']) - 12} more")
        if plan["other"]:
            con.print(
                f"  {len(plan['other'])} unmapped file(s) will land at the "
                "package root — see MIGRATION.md after the move"
            )

        if args.dry_run:
            con.print("axi ext migrate: dry-run — disk untouched.")
            return 0

        proceed = args.yes or args.to_aeos_layout
        if not proceed:
            # Interactive TTY: prompt. Non-TTY: refuse and exit 2.
            if sys.stdin.isatty() and sys.stdout.isatty():
                try:
                    resp = input("Migrate now? [Y/n] ")
                except EOFError:
                    resp = ""
                if resp.strip().lower() not in {"", "y", "yes"}:
                    con.print("axi ext migrate: aborted by user.")
                    return 1
                proceed = True
            else:
                con.print(
                    "axi ext migrate: use --yes to confirm or "
                    "--to-aeos-layout to proceed"
                )
                return 2

        try:
            summary = migrate_to_aeos_layout(target)
        except (FileExistsError, FileNotFoundError) as exc:
            con.print(f"axi ext migrate: {exc}")
            return 1
        except Exception as exc:  # noqa: BLE001 — surface unknown shapes loudly
            con.print(
                f"axi ext migrate: failed — {exc}. "
                "See docs/working/prompt-extension-migration.md for the "
                "supported pre-AEOS shapes."
            )
            return 1

        con.print(
            f"axi ext migrate: moved {len(summary['moves'])} files into "
            f"{summary['new_root']}"
        )
        if summary["other"]:
            con.print(
                f"axi ext migrate: {len(summary['other'])} file(s) had no "
                "explicit mapping — see MIGRATION.md"
            )
        return 0


__all__ = ["MigrateProvider", "migrate_to_aeos_layout"]
