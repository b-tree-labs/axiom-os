# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Validator — second-pass gate that drops hallucinated and noisy findings."""

from __future__ import annotations

import re
from collections import defaultdict

from axiom.extensions.builtins.review.tools.findings import Finding

_NIT_FLOOR = 20

# Matches "@@ -old_start,old_count +new_start,new_count @@"
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
# Matches "+++ b/path"
_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)
# Matches "diff --git a/... b/..."
_DIFF_HEADER_RE = re.compile(r"^diff --git ", re.MULTILINE)


def _parse_diff_line_ranges(diff: str) -> dict[str, set[int]]:
    """Return a mapping of {path -> set of new-side line numbers in the diff}."""
    touched: dict[str, set[int]] = defaultdict(set)

    # Split into per-file sections at each "diff --git" header.
    sections = _DIFF_HEADER_RE.split(diff)

    for section in sections:
        if not section.strip():
            continue
        # Extract the +++ b/ path from this section.
        path_match = _FILE_RE.search(section)
        if not path_match:
            continue
        path = path_match.group(1)

        # Extract all hunks and record the new-side line numbers.
        for hunk_match in _HUNK_RE.finditer(section):
            start = int(hunk_match.group(1))
            count_str = hunk_match.group(2)
            count = int(count_str) if count_str is not None else 1
            for ln in range(start, start + max(count, 1)):
                touched[path].add(ln)

    return dict(touched)


def validate(findings: list[Finding], diff: str) -> list[Finding]:
    """Filter findings to those whose path:line appears in the diff.

    Rules applied in order:
    1. Drop findings whose path is not in the diff at all.
    2. If finding.line is set, drop it unless the line is within ±2 of any
       line in the diff's touched range for that file.
    3. Cap nit findings at _NIT_FLOOR; drop all nits beyond that count.
    """
    if not findings:
        return []

    line_ranges = _parse_diff_line_ranges(diff)
    touched_paths = set(line_ranges.keys())

    result: list[Finding] = []
    nit_count = 0

    for finding in findings:
        # Rule 1: path must be in the diff.
        if finding.path not in touched_paths:
            continue

        # Rule 2: if a line is specified, it must be within ±2 of a diff line.
        if finding.line is not None:
            touched_lines = line_ranges.get(finding.path, set())
            fuzz = 2
            in_range = any(
                abs(finding.line - tl) <= fuzz for tl in touched_lines
            )
            if not in_range:
                continue

        # Rule 3: nit noise floor.
        if finding.severity == "nit":
            nit_count += 1
            if nit_count > _NIT_FLOOR:
                continue

        result.append(finding)

    return result


__all__ = ["validate"]
