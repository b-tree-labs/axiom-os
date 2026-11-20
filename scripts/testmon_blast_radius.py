#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""How many tests a ``pytest --testmon --collect-only`` summary selected.

The unit job's pull-request gate used to read the first integer on the last
line. Two summaries then lied:

- ``no tests collected (2 deselected)`` has no leading integer, so the gate
  treated an empty selection as unreadable and ran the whole suite.
- ``2/4 tests collected (2 deselected)`` is two tests, and the ``2`` inside
  the deselected tail is not the selection.

Print ``n=<count>`` and exit 0 when the summary is readable. Exit 2 when it
is not, so the caller runs the whole suite instead of trusting a guess.

``--decide`` makes the whole pull-request choice: a narrowed run, or the full
suite with the reason on one line. Testmon follows the Python a test executes.
It does not see configuration, dependency manifests or data files, so a
change to any of those since the map's commit runs every test.

The pre-push hook makes the same choice with a local map. ``--seed`` gives a new
worktree a sibling's map, ``--map-commit`` names the commit a map was built at,
and ``--docs-only`` applies the rule CI uses to skip the unit suite.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_SUMMARY = re.compile(r"(no tests collected|\d+\S* tests collected)")


def selected_count(text: str) -> int | None:
    """Selected tests, or None when the output has no collect summary."""
    summary = None
    for raw in text.splitlines():
        line = raw.strip().strip("=").strip()
        if _SUMMARY.search(line):
            summary = line
    if summary is None:
        return None
    if "no tests collected" in summary:
        return 0
    match = re.match(r"(\d+)", summary)
    if match is None:
        return None
    return int(match.group(1))


def total_count(text: str) -> int | None:
    """Tests collected before deselection: the ``M`` in ``N/M tests collected``."""
    summary = None
    for raw in text.splitlines():
        line = raw.strip().strip("=").strip()
        if _SUMMARY.search(line):
            summary = line
    if summary is None or "no tests collected" in summary:
        return None
    ratio = re.match(r"\d+/(\d+) tests collected", summary)
    if ratio:
        return int(ratio.group(1))
    plain = re.match(r"(\d+) tests collected", summary)
    return int(plain.group(1)) if plain else None


def map_tests(database: Path) -> int | None:
    """Distinct tests the map holds: the suite the cap is a fraction of."""
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT COUNT(DISTINCT test_name) FROM test_execution"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    return int(row[0]) if row and row[0] else None


_MAP_SHA = re.compile(r"^testmon-(?:sha-)?.+-([0-9a-f]{40})$")


def map_sha(key: str) -> str | None:
    """The commit a restored map was written on, from its cache key."""
    match = _MAP_SHA.match(key.strip())
    return match.group(1) if match else None


# Files testmon cannot see through. A change to one of them can break a test
# whose executed Python is unchanged, so it runs every test.
_CONFIG_NAMES = {
    "conftest.py": "a conftest changed",
    "pyproject.toml": "pyproject changed (pytest config or dependencies)",
    "setup.py": "packaging changed",
    "setup.cfg": "packaging or pytest config changed",
    "pytest.ini": "pytest config changed",
    "tox.ini": "pytest config changed",
    "uv.lock": "a dependency lock changed",
    "poetry.lock": "a dependency lock changed",
    "Pipfile.lock": "a dependency lock changed",
}
_FIXTURE_DIRS = {"fixtures", "fixture", "testdata", "test_data"}


_VERSION_LINE = re.compile(r'^[+-]\s*version\s*=\s*"[^"]*"\s*$')


def only_the_version_changed(diff_text: str) -> bool:
    """A ``git diff -U0`` of a pyproject that moves ``version`` and nothing else.

    A release bumps the version several times a day. That changes no pytest
    option and no dependency, so it is not a reason to run every test.
    """
    changed = [
        line
        for line in diff_text.splitlines()
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]
    return bool(changed) and all(_VERSION_LINE.match(line) for line in changed)


def git_version_only(map_commit: str) -> Callable[[str], bool]:
    """``only_the_version_changed`` for one path, read from git."""

    def check(path: str) -> bool:
        result = subprocess.run(
            ["git", "diff", "-U0", map_commit, "HEAD", "--", path],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0 and only_the_version_changed(result.stdout)

    return check


def full_suite_trigger(
    paths: list[str], version_only: Callable[[str], bool] | None = None
) -> str | None:
    """Why this diff must run every test, or None when testmon's map covers it."""
    for raw in paths:
        path = PurePosixPath(raw.strip())
        if not path.name:
            continue
        if path.name == "pyproject.toml" and version_only is not None and version_only(raw.strip()):
            continue
        reason = _CONFIG_NAMES.get(path.name)
        if reason is None and re.fullmatch(r"(requirements|constraints)[\w.-]*\.txt", path.name):
            reason = "a requirements file changed"
        if reason is None and path.suffix != ".py" and _FIXTURE_DIRS & set(path.parts[:-1]):
            reason = "a fixture data file changed"
        if reason:
            return f"{reason}: {raw.strip()}"
    return None


@dataclass(frozen=True)
class Decision:
    mode: str  # "blast" or "full"
    n: int | None
    total: int | None
    reason: str


def decide(
    collect_text: str | None,
    map_key: str,
    changed_files: list[str] | None,
    cap_fraction: float,
    database: Path | None = None,
    version_only: Callable[[str], bool] | None = None,
) -> Decision:
    """Narrow only when the map, the diff and the selection can all be trusted."""

    def full(reason: str, n: int | None = None, total: int | None = None) -> Decision:
        return Decision("full", n, total, f"full suite: {reason}")

    if not map_key.strip():
        return full("no blast-radius map was restored")
    sha = map_sha(map_key)
    if sha is None:
        return full(f"the restored map key names no commit ({map_key.strip()})")
    short = sha[:8]
    if changed_files is None:
        return full(f"could not diff this tree against map commit {short}")
    trigger = full_suite_trigger(changed_files, version_only)
    if trigger:
        return full(f"{trigger} (since map {short})")
    if collect_text is None:
        return full("testmon selection did not run")
    n = selected_count(collect_text)
    if n is None:
        return full("the testmon collect summary was unreadable")
    # The map's test count is the suite. The ``M`` in ``N/M tests collected``
    # is not: with selection on, testmon skips collecting whole files it
    # knows are unaffected (4289 of 24984 on #1417's first run).
    total = map_tests(database) if database is not None else None
    if total is None:
        total = total_count(collect_text)
    if not total:
        return full(f"selected {n}, but the suite size is unknown", n)
    cap = int(total * cap_fraction)
    share = f"{n} of {total} tests ({100 * n / total:.1f}%; cap {cap_fraction:.0%} = {cap})"
    if n > cap:
        return full(
            f"{share} is past the cap; a selection that broad is a cross-cutting change, "
            "and testmon does not see the config and data reads such a change also moves",
            n,
            total,
        )
    return Decision(
        "blast",
        n,
        total,
        f"narrowed: {share} against map {short}, {len(changed_files)} files changed",
    )


def main_text(text: str) -> int:
    count = selected_count(text)
    if count is None:
        return 2
    print(f"n={count}")
    return 0


def align_environment(database: Path, packages: str, python_version: str) -> int:
    """Rewrite stored package and interpreter fingerprints. Keep the test rows.

    pytest-testmon deletes an environment whose ``system_packages`` or
    ``python_version`` differ, and the replacement has no tests, so the next
    run selects the whole suite. A PyPI minor bump or a Python patch does
    that on a restored map. The blast radius is which source files changed.
    """
    connection = sqlite3.connect(database)
    try:
        cursor = connection.execute(
            "UPDATE environment SET system_packages = ?, python_version = ?",
            (packages, python_version),
        )
        connection.commit()
        return int(cursor.rowcount)
    finally:
        connection.close()


def current_fingerprint() -> tuple[str, str]:
    """The same strings pytest-testmon will compare on the next run."""
    from testmon.common import drop_patch_version, get_system_packages

    packages = drop_patch_version(get_system_packages())
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return packages, version


def docs_only(paths: list[str]) -> bool:
    """CI's rule: every changed path is under docs/ or is markdown."""
    changed = [path.strip() for path in paths if path.strip()]
    return bool(changed) and all(p.startswith("docs/") or p.endswith(".md") for p in changed)


MAP_NAME = ".testmondata"
COMMIT_NAME = ".testmondata.commit"


def _git(cwd: Path, *args: str) -> str | None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def map_commit(worktree: Path) -> str | None:
    """The commit a worktree's map was built at.

    A successful hook run records it. Otherwise the map's file time dates it,
    which errs early: an older commit means a wider diff, never a missed one.
    """
    recorded = worktree / COMMIT_NAME
    if recorded.is_file():
        commit = recorded.read_text().strip()
        if commit:
            return commit
    database = worktree / MAP_NAME
    if not database.is_file():
        return None
    written = int(database.stat().st_mtime)
    return _git(worktree, "rev-list", "-1", f"--before={written}", "HEAD") or None


def _worktrees(here: Path) -> list[Path]:
    listing = _git(here, "worktree", "list", "--porcelain") or ""
    return [Path(line[9:]) for line in listing.splitlines() if line.startswith("worktree ")]


def seed_map(here: Path) -> Path | None:
    """Copy the newest sibling map into a worktree with none, and name its source.

    Testmon compares content hashes, so a map from another branch is a diff,
    not a wrong answer. The copy goes through SQLite's backup, because the
    sibling may be writing it. Any failure leaves no map, and the caller runs
    every test.
    """
    target = here / MAP_NAME
    if target.exists():
        return None
    resolved = here.resolve()
    candidates = [
        tree.resolve()
        for tree in _worktrees(here)
        if tree.resolve() != resolved and (tree / MAP_NAME).is_file()
    ]
    candidates = [tree for tree in candidates if (tree / MAP_NAME).stat().st_size > 0]
    if not candidates:
        return None
    source = max(candidates, key=lambda tree: (tree / MAP_NAME).stat().st_mtime)
    commit = map_commit(source)
    if not commit:
        return None
    try:
        origin = sqlite3.connect(f"file:{source / MAP_NAME}?mode=ro", uri=True)
        copy = sqlite3.connect(target)
        try:
            origin.backup(copy)
        finally:
            copy.close()
            origin.close()
    except sqlite3.Error:
        for leftover in (target, here / f"{MAP_NAME}-wal", here / f"{MAP_NAME}-shm"):
            leftover.unlink(missing_ok=True)
        return None
    (here / COMMIT_NAME).write_text(commit + "\n")
    return source


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--align",
        metavar="DATABASE",
        help="rewrite .testmondata's environment fingerprint to this interpreter",
    )
    parser.add_argument(
        "--decide", action="store_true", help="choose blast or full; reads collect output on stdin"
    )
    parser.add_argument("--map-key", default="", help="the cache key the map was restored from")
    parser.add_argument(
        "--changed-files",
        metavar="PATH",
        help="files changed since the map's commit, one per line; absent means the diff failed",
    )
    parser.add_argument("--no-selection", action="store_true", help="the collect pass did not run")
    parser.add_argument("--cap-fraction", type=float, default=1 / 3)
    parser.add_argument("--database", default=".testmondata")
    parser.add_argument(
        "--docs-only", action="store_true", help="exit 0 when the paths on stdin are all docs"
    )
    parser.add_argument("--seed", action="store_true", help="copy a sibling's map if this has none")
    parser.add_argument(
        "--map-commit",
        action="store_true",
        help="print the commit this worktree's map was built at",
    )
    args = parser.parse_args(argv)
    if args.docs_only:
        return 0 if docs_only(sys.stdin.read().splitlines()) else 1
    if args.seed:
        source = seed_map(Path.cwd())
        if source:
            print(f"seeded the blast-radius map from {source.name}")
        return 0
    if args.map_commit:
        commit = map_commit(Path.cwd())
        if commit:
            print(commit)
            return 0
        return 1
    if args.decide:
        changed = None
        if args.changed_files and Path(args.changed_files).exists():
            changed = [
                line for line in Path(args.changed_files).read_text().splitlines() if line.strip()
            ]
        text = None if args.no_selection else sys.stdin.read()
        commit = map_sha(args.map_key)
        decision = decide(
            text,
            args.map_key,
            changed,
            args.cap_fraction,
            Path(args.database),
            git_version_only(commit) if commit else None,
        )
        print(f"testmon decision: {decision.reason}")
        print(f"mode={decision.mode}")
        print(f"n={'' if decision.n is None else decision.n}")
        return 0
    if args.align:
        packages, version = current_fingerprint()
        updated = align_environment(Path(args.align), packages, version)
        print(f"aligned {updated} environment row(s) to Python {version}")
        return 0
    return main_text(sys.stdin.read())


if __name__ == "__main__":
    sys.exit(main())
