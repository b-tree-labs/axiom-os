#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Name the tests a pre-push run failed, so the hook can run them once more alone.

Reads pytest's short summary (``FAILED <test> - <why>`` and ``ERROR <test> - <why>``) from the
run's saved output. Prints one test per line and exits 0 when there are between one and
``--limit`` of them. Exits 1 and prints nothing when there are none, or more than the limit: that
many failures are a real break, and running them again only costs time.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_LIMIT = 10

# The summary line starts the line. A progress line (``[gw0] [ 0%] FAILED ...``) does not.
_SUMMARY = re.compile(r"^(?:FAILED|ERROR) (.+?)(?: - .*)?$")


def failed_tests(text: str) -> list[str]:
    """The failed and errored tests named in the summary, once each, in the order given."""
    seen: dict[str, None] = {}
    for line in text.splitlines():
        hit = _SUMMARY.match(line.rstrip())
        if hit:
            seen.setdefault(hit.group(1).strip(), None)
    return list(seen)


def rerun_set(text: str, *, limit: int = DEFAULT_LIMIT) -> list[str] | None:
    """The tests to run again, or None when there are none or too many to be worth it."""
    tests = failed_tests(text)
    if not tests or len(tests) > limit:
        return None
    return tests


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("log", type=Path, help="the saved output of the test run")
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT, help="rerun at most this many tests"
    )
    args = parser.parse_args(argv)
    try:
        text = args.log.read_text(errors="replace")
    except OSError:
        return 1
    tests = rerun_set(text, limit=args.limit)
    if tests is None:
        return 1
    print("\n".join(tests))
    return 0


if __name__ == "__main__":
    sys.exit(main())
