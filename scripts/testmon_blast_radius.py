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
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--align",
        metavar="DATABASE",
        help="rewrite .testmondata's environment fingerprint to this interpreter",
    )
    args = parser.parse_args(argv)
    if args.align:
        packages, version = current_fingerprint()
        updated = align_environment(Path(args.align), packages, version)
        print(f"aligned {updated} environment row(s) to Python {version}")
        return 0
    return main_text(sys.stdin.read())


if __name__ == "__main__":
    sys.exit(main())
