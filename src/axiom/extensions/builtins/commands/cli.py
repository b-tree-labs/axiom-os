# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`axi commands` — generate cross-harness slash-command shims.

Subcommands:
  generate    Emit shims for one or more harnesses (--harness X[,Y,...] or all)
  list        Show the current rollup of nouns/verbs/slash commands
  regenerate  Refresh shims for every previously-generated harness
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from axiom.infra.paths import get_user_state_dir

from . import state
from .discovery import CommandTree, discover_command_tree
from .renderers import claude, codex, cursor, neovim, opencode, vim, vscode

HARNESSES = {
    "claude": claude.render,
    "cursor": cursor.render,
    "codex": codex.render,
    "vscode": vscode.render,
    "opencode": opencode.render,
    "neovim": neovim.render,
    "vim": vim.render,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="axi commands",
        description="Cross-harness slash-command generator",
    )
    sub = p.add_subparsers(dest="action")

    gen = sub.add_parser("generate", help="Emit shims for one or more harnesses")
    gen.add_argument(
        "--harness",
        default="all",
        help=f"Comma-separated list, or 'all'. Choices: {','.join(HARNESSES)}",
    )
    gen.add_argument(
        "--out-dir",
        type=Path,
        default=Path.cwd(),
        help="Root directory shims are emitted under (default: cwd)",
    )
    gen.add_argument(
        "--strict",
        action="store_true",
        help="Treat any noun/slash conflict as an error (default: warn)",
    )
    gen.add_argument(
        "--dry-run", action="store_true", help="Compute the tree but write nothing"
    )

    listp = sub.add_parser("list", help="Show the current command rollup")
    listp.add_argument(
        "--conflicts",
        action="store_true",
        help="Also surface shadowed (losing) definitions",
    )

    sub.add_parser(
        "regenerate",
        help="Refresh shims for every previously-generated harness",
    )

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.action:
        build_parser().print_help()
        return 1

    if args.action == "generate":
        return _cmd_generate(args)
    if args.action == "list":
        return _cmd_list(args)
    if args.action == "regenerate":
        return _cmd_regenerate(args)
    return 1


def _resolve_harnesses(spec: str) -> list[str]:
    if spec == "all":
        return list(HARNESSES.keys())
    chosen = [h.strip() for h in spec.split(",") if h.strip()]
    bad = [h for h in chosen if h not in HARNESSES]
    if bad:
        print(f"Unknown harness(es): {bad}. Choices: {list(HARNESSES)}", file=sys.stderr)
        sys.exit(2)
    return chosen


def _cmd_generate(args: argparse.Namespace) -> int:
    tree = discover_command_tree()

    if tree.conflicts:
        print(f"Note: {len(tree.conflicts)} conflict(s) resolved per tier policy:")
        for c in tree.conflicts:
            print(
                f"  {c.key:<20} winner={c.winner_extension:<15} "
                f"loser={c.loser_extension:<15} ({c.reason})"
            )
        if args.strict:
            print("--strict: aborting due to conflicts.", file=sys.stderr)
            return 2

    harnesses = _resolve_harnesses(args.harness)
    state_dir = get_user_state_dir()
    total_files = 0

    for h in harnesses:
        renderer = HARNESSES[h]
        if args.dry_run:
            # Render to a throwaway tmp to count what would be written
            import tempfile

            with tempfile.TemporaryDirectory() as td:
                files = renderer(tree, Path(td))
            print(f"  [dry-run] {h:<10} would write {len(files)} file(s)")
            continue
        files = renderer(tree, args.out_dir)
        state.upsert(state_dir, h, args.out_dir, len(files))
        total_files += len(files)
        print(f"  [ok] {h:<10} wrote {len(files)} file(s) under {args.out_dir}")

    if not args.dry_run:
        print(f"\nGenerated {total_files} file(s) across {len(harnesses)} harness(es).")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    tree: CommandTree = discover_command_tree()
    print(f"CLI nouns ({len(tree.nouns)}):")
    for noun, cli_noun in sorted(tree.nouns.items()):
        verbs = ", ".join(v.name for v in cli_noun.verbs) or "(no verbs introspected)"
        print(f"  {noun:<15} [{cli_noun.tier:<8} {cli_noun.extension}]  {verbs}")
    print(f"\nChat slash commands ({len(tree.slash_commands)}):")
    for name, sc in sorted(tree.slash_commands.items()):
        print(f"  /{name:<14} {sc.description}")
    if args.conflicts:
        print(f"\nConflicts ({len(tree.conflicts)}):")
        for c in tree.conflicts:
            print(
                f"  {c.key:<20} winner={c.winner_extension} "
                f"shadowed={c.loser_extension} ({c.reason})"
            )
    return 0


def _cmd_regenerate(args: argparse.Namespace) -> int:
    """Re-render every harness in state. Called by `axi update` after upgrade."""
    del args
    state_dir = get_user_state_dir()
    entries = state.load(state_dir)
    if not entries:
        print("No previously-generated shims to refresh.")
        return 0

    tree = discover_command_tree()
    for entry in entries:
        renderer = HARNESSES.get(entry.harness)
        if renderer is None:
            print(
                f"  [skip] {entry.harness} — renderer no longer available",
                file=sys.stderr,
            )
            continue
        files = renderer(tree, Path(entry.out_dir))
        state.upsert(state_dir, entry.harness, Path(entry.out_dir), len(files))
        print(
            f"  [refreshed] {entry.harness:<10} {len(files)} file(s) at {entry.out_dir}"
        )
    return 0


def regenerate_all() -> None:
    """Programmatic entry-point used by `axi update`."""
    _cmd_regenerate(argparse.Namespace())


if __name__ == "__main__":
    sys.exit(main())
