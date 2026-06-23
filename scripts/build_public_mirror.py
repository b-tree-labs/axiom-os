#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Build / verify the public Axiom mirror (ADR-078).

The public repository is a generated mirror of the private source-of-truth tree
minus everything listed in ``mirror/exclude.txt`` (gitignore-style). This script
is the single source of truth for two things:

  1. **What is public** — ``public_files()`` returns the tracked files that ship
     to the public mirror (tracked minus excluded).
  2. **What must never be public** — ``scan_forbidden()`` asserts that no public
     file leaks an institution/consumer identifier, personal path, or credential
     hint. ``tests/test_mirror.py`` runs this so regressions fail CI.

Usage:
    python scripts/build_public_mirror.py --verify          # check only (CI/test)
    python scripts/build_public_mirror.py --out DIR         # materialize the tree
    python scripts/build_public_mirror.py --list-excluded   # show excluded files
"""
from __future__ import annotations

import argparse
import fnmatch
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXCLUDE_FILE = REPO_ROOT / "mirror" / "exclude.txt"

# Terms that must NOT appear in any public (non-excluded) file. The genericization
# work (PRs #553–#559) removed these from the public surface; this list keeps them
# out. Matched case-insensitively per line, excluding the allowlisted contexts below.
FORBIDDEN_TERMS = (
    r"neutron[_-]os",     # the named domain consumer (any form)
    r"neutronos",
    r"utexas",            # institution identifier in content
    r"\bTACC\b",
    r"austin\.utexas",
    r"rascal-llm",        # credential-shaped hint
    r"rascal",            # internal host codename (any form) (use a generic name)
    r"10\.159\.142\.",    # internal host subnet (private IP must not be published)
    r"/Users/ben\b",      # personal filesystem path
    r"/home/ben\b",
    r"ben-macbook",
)
_FORBIDDEN_RE = re.compile("|".join(FORBIDDEN_TERMS), re.IGNORECASE)

# Legitimate occurrences that are NOT drift (legal attribution, guard tests that
# assert these terms stay OUT, the mirror tooling itself, filename cross-refs).
_ALLOW_LINE = re.compile(
    r"Copyright \(c\) 2026 The University of Texas"          # copyright headers
    r"|University of Texas at Austin and B-Tree"
)
_ALLOW_PATHS = {
    "NOTICE",                                                # legal/trademark file
    "mirror/exclude.txt",
    "scripts/build_public_mirror.py",
    "tests/test_mirror.py",
    "docs/adrs/adr-078-public-private-mirror.md",
    # Guard tests whose whole point is asserting these terms are absent:
    "src/axiom/extensions/builtins/release/tests/test_ci_repos_config.py",
    "tests/test_branding_package_name.py",
    "tests/infra/test_routing_health.py",
    "tests/rag/test_health.py",
    "tests/routing/test_ec_block_message.py",
}
# Per-file allowance for legitimate residue (real paper-author affiliations, an
# IP-ownership statement, a launchd/db filename reference, etc.).
_ALLOW_SUBSTR = {
    "docs/papers": ("University of Texas",),                 # author affiliations
    "docs/prds/prd-vega.md": ("University of Texas",),       # IP-owner statement
    "packages/axiom-ext-data-platform/pyproject.toml": ("University of Texas",),
}


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"],
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    return [p for p in out if p]


def _load_exclude_patterns() -> list[str]:
    patterns = []
    for raw in EXCLUDE_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns


def _matches(path: str, pattern: str) -> bool:
    """Minimal gitignore-style match: ``dir/`` prefixes, exact paths, globs."""
    if pattern.endswith("/"):
        return path == pattern[:-1] or path.startswith(pattern)
    if path == pattern:
        return True
    return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, pattern + "/*")


def excluded_files() -> list[str]:
    patterns = _load_exclude_patterns()
    return [p for p in _tracked_files() if any(_matches(p, pat) for pat in patterns)]


def public_files() -> list[str]:
    excluded = set(excluded_files())
    return [p for p in _tracked_files() if p not in excluded]


def stale_exclude_patterns() -> list[str]:
    """Exclude patterns that match no tracked file (rot detector)."""
    tracked = _tracked_files()
    stale = []
    for pat in _load_exclude_patterns():
        if not any(_matches(p, pat) for p in tracked):
            stale.append(pat)
    return stale


def _is_allowed(path: str, line: str) -> bool:
    if path in _ALLOW_PATHS or _ALLOW_LINE.search(line):
        return True
    for key, subs in _ALLOW_SUBSTR.items():
        if path.startswith(key) and any(s in line for s in subs):
            return True
    return False


def scan_forbidden(paths: list[str] | None = None) -> list[tuple[str, int, str]]:
    """Return (path, lineno, line) for forbidden terms in public files."""
    paths = paths if paths is not None else public_files()
    hits: list[tuple[str, int, str]] = []
    for rel in paths:
        f = REPO_ROOT / rel
        try:
            text = f.read_text(errors="replace")
        except (OSError, UnicodeError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if _FORBIDDEN_RE.search(line) and not _is_allowed(rel, line):
                hits.append((rel, i, line.strip()))
    return hits


def materialize(out_dir: Path) -> int:
    out_dir = out_dir.resolve()
    if out_dir.exists():
        shutil.rmtree(out_dir)
    n = 0
    for rel in public_files():
        dst = out_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / rel, dst)
        n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build/verify the public Axiom mirror (ADR-078).")
    ap.add_argument("--out", type=Path, help="materialize the public tree into this dir")
    ap.add_argument("--verify", action="store_true", help="fail if anything would leak")
    ap.add_argument("--list-excluded", action="store_true", help="list excluded files")
    args = ap.parse_args(argv)

    if args.list_excluded:
        for p in excluded_files():
            print(p)
        return 0

    stale = stale_exclude_patterns()
    hits = scan_forbidden()
    pub, exc = len(public_files()), len(excluded_files())
    print(f"public files: {pub}  |  excluded: {exc}")
    if stale:
        print(f"\n⚠ {len(stale)} stale exclude pattern(s) (match no tracked file):")
        for s in stale:
            print(f"  - {s}")
    if hits:
        print(f"\n✗ {len(hits)} forbidden-term leak(s) in public files:")
        for path, ln, line in hits[:40]:
            print(f"  {path}:{ln}: {line[:100]}")

    if args.out:
        n = materialize(args.out)
        print(f"\nmaterialized {n} files → {args.out}")

    if args.verify and (stale or hits):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
