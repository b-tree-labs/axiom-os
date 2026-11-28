#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Which changed paths make a pull request take main's full CI path (#2441).

``scripts/ci_full_paths.json`` holds the label and the glob patterns; this file only reads it.
The workflow's change job pipes the pull request's changed paths in::

    git diff --name-only "$base" "$head" | python3 scripts/ci_full_label.py paths

and gets back the paths that match, one per line (nothing when none do; the exit status is
zero either way). ``label`` prints the label name. Standard library only, so it runs before
any dependency is installed.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DATA = Path(__file__).with_name("ci_full_paths.json")


def _regex(pattern: str) -> re.Pattern[str]:
    """GitHub's glob: ``**`` crosses directories, ``*`` stays inside one."""
    out = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out)


def load() -> dict:
    return json.loads(DATA.read_text())


def matches(path: str, patterns: list[str] | None = None) -> bool:
    patterns = load()["patterns"] if patterns is None else patterns
    return any(_regex(p).fullmatch(path) for p in patterns)


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else ""
    if command == "label":
        print(load()["label"])
        return 0
    if command == "paths":
        patterns = load()["patterns"]
        for line in sys.stdin.read().splitlines():
            path = line.strip()
            if path and matches(path, patterns):
                print(path)
        return 0
    print("usage: ci_full_label.py paths|label", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
