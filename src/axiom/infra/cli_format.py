# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Reusable CLI formatting helpers for consistent terminal output.

Provides section headers, separators, Unicode boxes, and key-value lines
so every extension renders with the same visual style.
"""

from __future__ import annotations


def section_header(title: str, char: str = "=", width: int = 80) -> str:
    """Return a section header string.

    Example output::

        ════════════════════════════════════════════════════════════════════════════════
        Title Here
        ════════════════════════════════════════════════════════════════════════════════
    """
    rule = char * width
    return f"{rule}\n{title}\n{rule}"


def separator(char: str = "─", width: int = 80) -> str:
    """Return a horizontal separator line."""
    return char * width


def box(title: str, rows: list[str], width: int = 52) -> str:
    """Return a Unicode box.

    Example output::

        ╭─ Title ──────────────────────────────────────────╮
        │  Row 1                                           │
        │  Row 2                                           │
        ╰──────────────────────────────────────────────────╯

    *width* is the total outer width including the border characters.
    """
    # Top border: ╭─ Title ─…─╮
    inner = width - 2  # space between ╭ and ╮
    title_segment = f"─ {title} "
    top = "╭" + title_segment + "─" * (inner - len(title_segment)) + "╮"

    # Content rows
    body_lines: list[str] = []
    for row in rows:
        # Pad or truncate to fit inside the box
        padded = f"  {row}"
        if len(padded) > inner:
            padded = padded[: inner - 1] + "…"
        body_lines.append("│" + padded.ljust(inner) + "│")

    # Bottom border
    bottom = "╰" + "─" * inner + "╯"

    return "\n".join([top, *body_lines, bottom])


def kv_line(key: str, value: str, key_width: int = 20) -> str:
    """Return a formatted key-value line.

    Example::

        Key:                Value
    """
    label = f"{key}:"
    return f"  {label:<{key_width}}{value}"
