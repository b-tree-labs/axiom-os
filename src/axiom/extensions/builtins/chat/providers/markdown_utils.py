# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Shared markdown utilities for render providers.

Terminal width detection, code block extraction, diff detection.
"""

from __future__ import annotations

import re


def terminal_width(cap: int = 120) -> int:
    """Get terminal width, capped for readability."""
    try:
        import shutil

        return min(shutil.get_terminal_size().columns, cap)
    except Exception:
        return 80


_DIFF_HEADER_RE = re.compile(r"^(---|\+\+\+)\s")
_DIFF_HUNK_RE = re.compile(r"^@@\s")


def is_diff(text: str) -> bool:
    """Detect if text looks like unified diff format."""
    lines = text.splitlines()
    if len(lines) < 3:
        return False
    diff_markers = sum(
        1
        for line in lines
        if line.startswith(("+", "-", "@@")) or _DIFF_HEADER_RE.match(line) is not None
    )
    return diff_markers / max(len(lines), 1) > 0.3


_CODE_FENCE_RE = re.compile(r"^```(\w*)")


def extract_code_blocks(text: str) -> list[tuple[str, str]]:
    """Extract fenced code blocks as (language, code) tuples."""
    blocks = []
    lines = text.splitlines()
    in_block = False
    lang = ""
    block_lines: list[str] = []

    for line in lines:
        m = _CODE_FENCE_RE.match(line.strip())
        if m and not in_block:
            in_block = True
            lang = m.group(1) or ""
            block_lines = []
        elif line.strip() == "```" and in_block:
            in_block = False
            blocks.append((lang, "\n".join(block_lines)))
        elif in_block:
            block_lines.append(line)

    return blocks


def truncate_result(text: str, max_lines: int = 5) -> str:
    """Truncate text to max_lines, showing count of hidden lines."""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    shown = lines[: max_lines - 1]
    remaining = len(lines) - len(shown)
    shown.append(f"... ({remaining} more lines)")
    return "\n".join(shown)
