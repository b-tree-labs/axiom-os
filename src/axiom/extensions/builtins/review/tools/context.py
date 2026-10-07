# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Context tool — reads full file contents for diff-touched files."""

from __future__ import annotations

import re
from pathlib import Path

from .findings import Finding

_MAX_FILES = 50

# Matches "diff --git a/path b/path" and "+++ b/path" lines.
_DIFF_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)
# Binary file sentinel in git diff output.
_BINARY_RE = re.compile(r"^Binary files ", re.MULTILINE)


def _parse_diff_paths(diff: str) -> list[str]:
    """Extract relative file paths from a unified diff."""
    return _DIFF_FILE_RE.findall(diff)


def gather_context(
    diff: str, repo_root: str = "."
) -> tuple[dict[str, str], list[Finding]]:
    """Return full file contents for every file touched by *diff*.

    Returns:
        (context_map, warnings) where context_map maps relative path → full
        file text.  If more than 50 files are touched, context_map is empty
        and warnings contains a single Finding describing the cap.

    Binary files and symlinks are silently skipped.
    Missing files (deleted in the diff) are silently skipped.
    """
    paths = _parse_diff_paths(diff)
    warnings: list[Finding] = []

    if len(paths) > _MAX_FILES:
        return {}, [
            Finding(
                severity="minor",
                pass_kind="correctness",
                path="",
                line=None,
                message=(
                    f"diff too large for single-pass review: "
                    f"{len(paths)} files touched (cap is {_MAX_FILES})"
                ),
                suggested_fix="Split the diff into smaller chunks or use --pass to run individual passes.",
            )
        ]

    root = Path(repo_root)
    context: dict[str, str] = {}

    for rel_path in paths:
        full_path = root / rel_path
        # Skip symlinks.
        if full_path.is_symlink():
            continue
        # Skip missing files (e.g. deleted).
        if not full_path.exists():
            continue
        # Skip binary files — check if the diff marks them as binary.
        # Also skip by trying to decode; if it fails it's binary.
        try:
            text = full_path.read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, IsADirectoryError):
            continue
        context[rel_path] = text

    return context, warnings


__all__ = ["gather_context"]
