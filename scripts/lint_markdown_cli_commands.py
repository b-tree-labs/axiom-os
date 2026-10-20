#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Markdown CLI-command linter (CI gate).

Scans every ``*.md`` under the given paths for fenced bash blocks,
parses each line that starts with ``axi `` against the canonical
vocabulary built from the installed extensions, and exits non-zero on
any unknown noun / verb / flag.

Usage::

    python scripts/lint_markdown_cli_commands.py docs/ README.md
    python scripts/lint_markdown_cli_commands.py --root .

The intent is to wire this into CI on both ``axiom-os`` and
a consumer repo so runbook drift stops being a time-bomb.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from axiom.infra.cli_vocabulary import build_vocabulary, lint_markdown


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lint_markdown_cli_commands")
    parser.add_argument("paths", nargs="*", default=["."],
                        help="files or directories to scan (default: .)")
    parser.add_argument("--root", default=".",
                        help="repo root for shorter error paths (default: .)")
    parser.add_argument("--exit-zero", action="store_true",
                        help="report findings but exit 0 (useful during rollout)")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    targets: list[Path] = []
    for raw in args.paths:
        p = Path(raw)
        if not p.exists():
            continue
        if p.is_file():
            targets.append(p)
        else:
            targets.extend(p.rglob("*.md"))

    targets = sorted({t.resolve() for t in targets})
    if not targets:
        print("no markdown files found", file=sys.stderr)
        return 0

    vocab = build_vocabulary()

    total = 0
    for md in targets:
        try:
            text = md.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel = md.relative_to(root) if md.is_relative_to(root) else md
        findings = lint_markdown(text, vocab=vocab, path=str(rel))
        for f in findings:
            print(f"{f.file}:{f.line} [{f.issue}] {f.command}")
            print(f"    → {f.detail}")
            total += 1

    if total == 0:
        print(f"OK: 0 findings across {len(targets)} markdown file(s)")
        return 0
    print(f"\n{total} finding(s) in markdown CLI commands", file=sys.stderr)
    return 0 if args.exit_zero else 1


if __name__ == "__main__":
    sys.exit(main())
