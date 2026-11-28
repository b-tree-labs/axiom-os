# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Is this commit nothing but a version bump? (#2441 item 10, ADR-289)

A tag needs a completed green main run on its exact commit. A release commit changes
the project version and the pins that follow it, and nothing else, so when its parent
has a green main run the unit suite would only re-prove the parent. The owner approved
skipping it for exactly that case on 2026-11-25. The wheel build, the prerelease wheel
check and the publish checks still run.

This is the strict half. A commit qualifies only when, comparing it with its single
parent:

- every changed file is ``pyproject.toml`` (required) or one of the release pin files,
  each *modified*: no file added, deleted, renamed, copied or type-changed, no mode change;
- ``pyproject.toml`` changed exactly one line, ``version = "X.Y.Z"`` to a higher plain
  ``X.Y.Z``, and the parsed ``[project].version`` is the old value before and the new one after;
- every changed line in a pin file is the version pin of that file, differing from the line it
  replaces by exactly the old version becoming the new one.

It reads content, not names. Anything else, and any doubt (a merge commit, a diff it cannot
parse, a line it did not expect), does not qualify: the normal path runs. The CLI always
exits 0 and prints ``qualifies=true|false``, so a crash cannot read as a yes.

    python -m axiom.extensions.builtins.release.version_bump --head <sha>
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

PYPROJECT = "pyproject.toml"
#: The files that carry the release version besides pyproject, with the one line shape each may change.
PIN_FILES: dict[str, str] = {
    "actions/rivet-land/README.md": r"^\| `axiom-version` \| `{v}` \|.*$",
    "actions/rivet-land/action.yml": r'^\s*default: "{v}"\s*$',
    "actions/rivet-land/caller-example.yml": r'^\s*# axiom-version: "{v}"\s*$',
}
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
_PYPROJECT_LINE = re.compile(r'^version = "(\d+\.\d+\.\d+)"$')
_HEADER = ("diff --git ", "index ", "--- ", "+++ ")


@dataclass(frozen=True)
class Verdict:
    qualifies: bool
    reason: str
    old: str = ""
    new: str = ""


def _no(reason: str) -> Verdict:
    return Verdict(False, reason)


def _parse_diff(diff: str) -> dict[str, tuple[list[str], list[str]]] | str:
    """File -> (removed lines, added lines), or the reason the diff is not plain line edits."""
    files: dict[str, tuple[list[str], list[str]]] = {}
    current: tuple[list[str], list[str]] | None = None
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" ")
            if len(parts) != 4 or parts[2][2:] != parts[3][2:] or parts[2][:2] != "a/":
                return f"unreadable file header: {line}"
            name = parts[3][2:]
            if name in files:
                return f"{name} appears twice"
            current = files[name] = ([], [])
            in_hunk = False
        elif line.startswith("@@ "):
            if current is None:
                return "a hunk before any file"
            in_hunk = True
        elif in_hunk and line[:1] in {"-", "+"}:
            assert current is not None
            (current[0] if line[0] == "-" else current[1]).append(line[1:])
        elif not in_hunk and line.startswith(_HEADER):
            continue
        else:
            return f"unexpected diff line: {line[:60]!r}"
    return files


def _project_version(text: str) -> str | None:
    try:
        value = tomllib.loads(text)["project"]["version"]
    except (tomllib.TOMLDecodeError, KeyError, TypeError):
        return None
    return value if isinstance(value, str) else None


def classify(
    name_status: str,
    diff: str,
    parent_pyproject: str,
    head_pyproject: str,
    parents: int = 1,
) -> Verdict:
    """Does this diff against the parent change only version strings?"""
    if parents != 1:
        return _no(f"the commit has {parents} parents; a bump has exactly one")
    statuses: dict[str, str] = {}
    for raw in name_status.splitlines():
        status, tab, path = raw.partition("\t")
        if not tab or not path or "\t" in path:
            return _no(f"unreadable change line: {raw!r}")
        statuses[path] = status
    if not statuses:
        return _no("no changed files")
    for path, status in statuses.items():
        if status != "M":
            return _no(f"{path} is not a plain modification ({status})")
        if path != PYPROJECT and path not in PIN_FILES:
            return _no(f"{path} is not a file a version bump may touch")
    if PYPROJECT not in statuses:
        return _no("pyproject.toml did not change: not a release")

    parsed = _parse_diff(diff)
    if isinstance(parsed, str):
        return _no(parsed)
    if set(parsed) != set(statuses):
        return _no("the changed files and the diff disagree")

    removed, added = parsed[PYPROJECT]
    if len(removed) != 1 or len(added) != 1:
        return _no(
            f"pyproject.toml changed {len(removed)} and {len(added)} lines; a bump changes one"
        )
    old_line, new_line = _PYPROJECT_LINE.match(removed[0]), _PYPROJECT_LINE.match(added[0])
    if not old_line or not new_line:
        return _no('the changed pyproject.toml line is not a plain version = "X.Y.Z" line')
    old, new = old_line.group(1), new_line.group(1)
    if tuple(map(int, _VERSION.match(new).groups())) <= tuple(
        map(int, _VERSION.match(old).groups())
    ):  # type: ignore[union-attr]
        return _no(f"{new} does not come after {old}")
    if _project_version(parent_pyproject) != old or _project_version(head_pyproject) != new:
        return _no("the changed line is not [project].version, or the files do not show it")

    for path, template in PIN_FILES.items():
        if path not in parsed:
            continue
        gone, came = parsed[path]
        if not gone or len(gone) != len(came):
            return _no(
                f"{path} changed {len(gone)} and {len(came)} lines; a pin changes one for one"
            )
        pattern = re.compile(template.replace("{v}", re.escape(old)))
        for before, after in zip(gone, came, strict=True):
            if not pattern.match(before):
                return _no(f"{path}: {before.strip()!r} is not the version pin of {old}")
            if before.replace(old, new, 1) != after:
                return _no(f"{path}: {after.strip()!r} is more than {old} becoming {new}")
    return Verdict(True, f"only version strings changed: {old} to {new}", old, new)


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False, timeout=60
    )
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {done.stderr.strip()[:200]}")
    return done.stdout


def check(root: Path, head: str) -> tuple[Verdict, str]:
    """Classify ``head`` against its parent in ``root``. Returns the verdict and the parent."""
    commit = _git(root, "rev-list", "--parents", "-n", "1", head).split()
    parent = commit[1] if len(commit) > 1 else ""
    if len(commit) != 2:
        return _no(f"{head} has {len(commit) - 1} parents; a bump has exactly one"), parent
    flags = ["--no-renames", "--no-color", "--no-ext-diff"]
    verdict = classify(
        _git(root, "diff", "--name-status", *flags, parent, commit[0]),
        _git(root, "diff", "-U0", *flags, parent, commit[0]),
        _git(root, "show", f"{parent}:{PYPROJECT}"),
        _git(root, "show", f"{commit[0]}:{PYPROJECT}"),
    )
    return verdict, parent


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--head", required=True)
    parser.add_argument("--root", default=".")
    args = parser.parse_args(argv)
    try:
        verdict, parent = check(Path(args.root), args.head)
    except Exception as error:  # fail closed: no answer is not a yes
        verdict, parent = _no(f"could not decide: {error}"), ""
    print(f"qualifies={'true' if verdict.qualifies else 'false'}")
    print(f"parent={parent}")
    print(f"reason={verdict.reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
