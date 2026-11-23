#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Lint + reserve ADR numbers.

Two failure modes this catches that have hit us twice in one week:

1. **Collision** — two parallel sessions both grab the next ADR number.
   Both PRs merge; main ends up with two ``adr-NNN-*.md`` files for the
   same NNN. Lint mode (default) detects this and exits non-zero.

2. **Drift** — author hand-picks an ADR number from memory ("I think
   047 was last") that's already taken. ``--next`` mode prints the
   next available number, so authors stop guessing.

Usage::

    python scripts/lint_adr_numbers.py            # lint mode (CI/pre-commit)
    python scripts/lint_adr_numbers.py --next     # print next available NNN

The lint mode is the load-bearing one — wire it into pre-commit + CI so
collisions never reach main again.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterable

ADR_DIR = Path(__file__).parent.parent / "docs" / "adrs"
# Matches ``adr-NNN-<rest>.md``; NNN is the captured 3+ digit number.
ADR_FILE_RE = re.compile(r"^adr-(\d{3,})-.+\.md$")


def remote_adr_numbers(repo_root: Path) -> set[int]:
    """ADR numbers visible on ``origin/*`` — the reservation set.

    ``--next`` computed from the LOCAL tree told two parallel branches the
    same number three times in one day (110, 109, 119): the number was
    effectively allocated at push time by whoever won the race, on a file
    written days earlier. Scanning every remote ref closes that window for
    any branch that has been pushed; two never-pushed worktrees can still
    collide, and the pre-push lint remains the backstop for that.

    Fail-soft: no git / no remote → empty set (never break ``--next``),
    but say so, because a silent fallback would quietly reopen the race.
    """
    try:
        subprocess.run(
            ["git", "-C", str(repo_root), "fetch", "origin", "--quiet"],
            check=False,
            capture_output=True,
            timeout=30,
        )
        refs = subprocess.run(
            ["git", "-C", str(repo_root), "for-each-ref", "--format=%(refname)", "refs/remotes/origin"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.split()
        numbers: set[int] = set()
        for ref in refs:
            listing = subprocess.run(
                ["git", "-C", str(repo_root), "ls-tree", "--name-only", ref, "docs/adrs/"],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout
            for line in listing.splitlines():
                m = ADR_FILE_RE.match(Path(line).name)
                if m:
                    numbers.add(int(m.group(1)))
        return numbers
    except Exception as exc:  # noqa: BLE001 — reservation is best-effort; the lint backstops
        print(
            f"⚠️  could not scan origin for reserved ADR numbers ({exc}); "
            "using the local tree only — a parallel pushed branch may hold "
            "the number this prints.",
            file=sys.stderr,
        )
        return set()


def worktree_adr_numbers(repo_root: Path, rel: Path = Path("docs/adrs")) -> dict[int, str]:
    """ADR numbers held by SIBLING WORKTREES on this machine — number → where.

    The third place a number can already be taken, and the only one neither of
    the other two scans can reach. ``find_adrs`` reads the local checkout;
    ``remote_adr_numbers`` reads every pushed ref. A draft that has been
    written and never ``git add``ed is in neither, and git — asked from any
    other worktree — will honestly tell you the number is free.

    That is not a hypothetical. ADR-142 through ADR-147 sat untracked in a
    sibling worktree while this checkout was told 142 was next, and the
    collision was found by hand afterwards. The docstring on
    ``remote_adr_numbers`` already named this case and left it to "the
    pre-push lint", which cannot see a sibling worktree either.

    So it reads the DIRECTORY, not git. Anything on disk in a worktree's
    ``docs/adrs`` counts, tracked or not, because what makes a number taken is
    that somebody is using it — not that they have committed yet.

    Returns where each one came from as well as which, because a number you
    cannot account for is a number the next session re-derives by hand.

    Fail-soft, like its sibling: no git, no worktrees, not a repo → ``{}``.
    A reservation scan that raised would make ``--next`` unusable on the
    machines that need it least.
    """
    try:
        listing = subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except Exception:  # noqa: BLE001 — see docstring; the lint backstops
        return {}

    here = repo_root.resolve()
    held: dict[int, str] = {}
    for line in listing.splitlines():
        if not line.startswith("worktree "):
            continue
        tree = Path(line[len("worktree ") :]).resolve()
        if tree == here:
            continue
        for number, path in find_adrs(tree / rel):
            # First writer wins the attribution; the number is taken either way.
            held.setdefault(number, f"{tree.name}/{path.name}")
    return held


def find_adrs(adr_dir: Path) -> list[tuple[int, Path]]:
    """Return ``[(number, path), ...]`` for every adr-NNN-*.md in ``adr_dir``."""
    out: list[tuple[int, Path]] = []
    if not adr_dir.is_dir():
        return out
    for p in sorted(adr_dir.iterdir()):
        if not p.is_file():
            continue
        m = ADR_FILE_RE.match(p.name)
        if m:
            out.append((int(m.group(1)), p))
    return out


def collisions(adrs: Iterable[tuple[int, Path]]) -> dict[int, list[Path]]:
    """Return ``{NNN: [path, path, ...]}`` for any NNN with >1 file."""
    by_n: dict[int, list[Path]] = defaultdict(list)
    for n, p in adrs:
        by_n[n].append(p)
    return {n: paths for n, paths in by_n.items() if len(paths) > 1}


#: The number in the first heading, e.g. ``# ADR-054 — ...``.
ADR_TITLE_RE = re.compile(r"^#\s*ADR[-\s](\d+)", re.IGNORECASE)


def title_mismatches(adrs: Iterable[tuple[int, Path]]) -> list[tuple[Path, int, int]]:
    """``[(path, filename_number, heading_number), ...]`` where they disagree.

    A collision check asks whether two files claim one number. This asks the
    other question: whether one file claims two. Both make a citation
    ambiguous, and this one is the quieter of the two — the file sorts
    correctly, the index looks right, and only a reader following
    "see ADR-054" into a document headed ADR-035 ever notices.

    It happens on renumbering: the file is renamed and the heading is not.
    Two ADRs in this tree were in that state when the check was added.
    """
    out: list[tuple[Path, int, int]] = []
    for number, path in adrs:
        try:
            first = path.read_text(encoding="utf-8").lstrip().splitlines()[0]
        except (OSError, IndexError):
            continue
        m = ADR_TITLE_RE.match(first)
        if m and int(m.group(1)) != number:
            out.append((path, number, int(m.group(1))))
    return out


def next_available(adrs: Iterable[tuple[int, Path]]) -> int:
    """Return ``max(NNN) + 1`` over the set, or ``1`` if empty.

    Intentionally does **not** fill gaps from deleted/renumbered ADRs —
    monotonically increasing numbers are easier to reason about than
    "the next gap." If you need to reuse a deleted number, do it
    explicitly.
    """
    nums = [n for n, _ in adrs]
    return max(nums) + 1 if nums else 1


def lint(adr_dir: Path = ADR_DIR) -> int:
    """Exit-code-style lint: 0 = clean, 1 = collision detected."""
    adrs = find_adrs(adr_dir)

    mismatched = title_mismatches(adrs)
    if mismatched:
        print(f"❌ ADR heading does not match its filename in {adr_dir}:\n", file=sys.stderr)
        for path, filed, titled in mismatched:
            print(f"   {path.name} is headed 'ADR-{titled:03d}'", file=sys.stderr)
            print(f"     → fix the heading to ADR-{filed:03d}, or rename the file", file=sys.stderr)
        print(
            "\n   One document claiming two numbers makes every citation to it "
            "ambiguous.\n", file=sys.stderr
        )
        return 1

    coll = collisions(adrs)
    if coll:
        print(
            f"❌ ADR number collision detected in {adr_dir}:\n", file=sys.stderr
        )
        for n, paths in sorted(coll.items()):
            print(f"   ADR-{n:03d} has {len(paths)} files:", file=sys.stderr)
            for p in paths:
                print(f"     - {p.name}", file=sys.stderr)
        print(
            "\n   Fix: rename the loser to the next available number "
            f"(currently ADR-{next_available(adrs):03d}); update every "
            "in-repo reference; bump CHANGELOG.\n",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--next",
        action="store_true",
        help="Print the next available ADR number (and exit 0) instead of linting.",
    )
    p.add_argument(
        "--adr-dir",
        type=Path,
        default=ADR_DIR,
        help=f"Path to docs/adrs (default: {ADR_DIR}).",
    )
    args = p.parse_args(argv)

    if args.next:
        adr_dir = args.adr_dir.resolve()
        repo_root = adr_dir.parent.parent
        rel = Path("docs/adrs")
        try:
            rel = adr_dir.relative_to(repo_root)
        except ValueError:  # an --adr-dir somewhere else entirely
            pass

        local = {n for n, _ in find_adrs(adr_dir)}
        pushed = remote_adr_numbers(repo_root)
        elsewhere = worktree_adr_numbers(repo_root, rel)

        nums = local | pushed | set(elsewhere)
        # Only the foreign reservations ABOVE the local maximum changed the
        # answer. Listing the rest would bury the one line that explains why
        # this number is not the one you expected.
        floor = max(local, default=0)
        moved = {n: where for n, where in elsewhere.items() if n > floor}
        if moved:
            print(
                "note: reserved in a sibling worktree on this machine — "
                + ", ".join(f"{n:03d} ({where})" for n, where in sorted(moved.items())),
                file=sys.stderr,
            )
        print(f"{(max(nums) + 1) if nums else 1:03d}")
        return 0

    return lint(args.adr_dir)


if __name__ == "__main__":
    raise SystemExit(main())
