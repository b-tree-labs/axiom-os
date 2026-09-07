# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`axi context` — keep every assistant's project-context file in sync.

Verbs:
  * ``axi context init``  — scaffold AGENTS.md + CLAUDE.md symlink, generate the
    per-tool files, install the pre-commit drift hook (adoption, ADR-051 §D).
  * ``axi context sync``  — (re)generate the per-tool files from AGENTS.md.
  * ``axi context check`` — fail (exit 1) if any generated file is missing or
    drifted; the pre-commit / CI gate.
"""

from __future__ import annotations

import argparse
import json

from . import core

_STATUS_GLYPH = {
    "created": "+",
    "written": "~",
    "unchanged": "=",
    "ok": "=",
    "drift": "!",
    "missing": "x",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axi context",
        description="Generate per-tool context files from the canonical AGENTS.md",
    )
    sub = parser.add_subparsers(dest="action")

    for verb, help_text in (
        ("init", "Scaffold AGENTS.md + generate tool files + install hook"),
        ("sync", "Generate the per-tool context files from AGENTS.md"),
        ("check", "Fail if any generated context file is missing or drifted"),
        ("status", "Surface 'this repo could upgrade' findings with their fixes"),
    ):
        p = sub.add_parser(verb, help=help_text)
        p.add_argument("--root", default="", help="Repo root (default: git toplevel or cwd)")
        p.add_argument("--format", choices=["human", "json"], default="human")
        if verb == "init":
            p.add_argument(
                "--force", action="store_true",
                help="Overwrite an existing AGENTS.md with the starter template",
            )

    return parser


def _resolve_root(args: argparse.Namespace) -> str:
    return args.root or str(core.repo_root_default())


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.action:
        build_parser().print_help()
        return 1
    root = _resolve_root(args)
    handler = {
        "init": _cmd_init,
        "sync": _cmd_sync,
        "check": _cmd_check,
        "status": _cmd_status,
    }[args.action]
    return handler(args, root)


def _cmd_status(args: argparse.Namespace, root: str) -> int:
    """`axi context status` — discovery surface; informational, always exit 0."""
    found = core.findings(root)
    if args.format == "json":
        print(json.dumps([f.to_dict() for f in found], indent=2))
        return 0
    if not found:
        print(f"context: in sync at {root}.")
        return 0
    for f in found:
        print(f"  [{f.severity}] {f.message}\n          → {f.remediation}")
    return 0


def _print_results(results: list[core.TargetResult]) -> None:
    for r in results:
        print(f"  [{_STATUS_GLYPH.get(r.status, '?')}] {r.target:<8} {r.path}  ({r.status})")


def _cmd_check(args: argparse.Namespace, root: str) -> int:
    results = core.check(root)
    if args.format == "json":
        print(json.dumps([r.to_dict() for r in results], indent=2))
    elif not results:
        print(f"No AGENTS.md at {root} — nothing to check. Run `axi context init`.")
        return 0
    else:
        _print_results(results)
    if core.has_drift(results):
        if args.format != "json":
            print("\ncontext files drifted from AGENTS.md — run `axi context sync`")
        return 1
    return 0


def _cmd_sync(args: argparse.Namespace, root: str) -> int:
    if core.read_canonical(root) is None:
        print(f"No AGENTS.md at {root}. Run `axi context init` first.")
        return 1
    results = core.sync(root, write=True)
    if args.format == "json":
        print(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        _print_results(results)
        changed = [r for r in results if r.status != "unchanged"]
        print(f"\n{len(changed)} file(s) updated, {len(results) - len(changed)} unchanged.")
    return 0


def _cmd_init(args: argparse.Namespace, root: str) -> int:
    res = core.init(root, force=args.force)
    if args.format == "json":
        print(json.dumps(res.to_dict(), indent=2))
        return 0
    if res.promoted_from_claude:
        agents_state = "promoted from existing CLAUDE.md"
    elif res.created_canonical:
        agents_state = "created (starter template)"
    else:
        agents_state = "kept existing"
    print(f"AGENTS.md: {agents_state}")
    print(f"CLAUDE.md symlink: {'created' if res.symlinked_claude else 'already present / skipped'}")
    print(f"pre-commit hook: {res.hook}")
    _print_results(res.sync_results)
    if res.created_canonical:
        print("\nNext: edit AGENTS.md, then `axi context sync`.")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
