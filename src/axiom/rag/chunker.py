# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Document chunking for RAG ingestion.

Splits markdown/text documents into overlapping chunks, preferring
paragraph and section boundaries over arbitrary character splits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Chunk:
    """A single chunk of a source document."""

    text: str
    source_path: str  # relative to repo root
    source_title: str
    chunk_index: int
    start_line: int
    source_type: str = "markdown"


def _extract_title(text: str) -> str:
    """Extract the first markdown heading from *text*."""
    for line in text.splitlines()[:15]:
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _find_break(text: str, max_pos: int) -> int:
    """Find the best break position in *text* up to *max_pos*.

    Preference order:
      1. Paragraph boundary (double newline)
      2. Sentence boundary (period / question-mark / exclamation followed by space/newline)
      3. Any newline
      4. max_pos as-is
    """
    region = text[:max_pos]

    # 1. Paragraph boundary
    idx = region.rfind("\n\n")
    if idx > max_pos // 3:
        return idx + 2  # include the double newline

    # 2. Sentence boundary
    m = None
    for m in re.finditer(r"[.!?]\s", region):
        pass  # advance to last match
    if m and m.end() > max_pos // 3:
        return m.end()

    # 3. Any newline
    idx = region.rfind("\n")
    if idx > max_pos // 3:
        return idx + 1

    return max_pos


def chunk_markdown(
    text: str,
    path: str,
    chunk_size: int = 800,
    overlap: int = 200,
) -> list[Chunk]:
    """Split *text* into overlapping chunks.

    Parameters
    ----------
    text:
        Full document content.
    path:
        Relative path to the source file (stored on each chunk).
    chunk_size:
        Target characters per chunk.
    overlap:
        Character overlap between consecutive chunks.

    Returns
    -------
    list[Chunk]
    """
    if not text.strip():
        return []

    title = _extract_title(text)
    source_type = "markdown" if path.endswith(".md") else "text"
    chunks: list[Chunk] = []
    pos = 0
    index = 0

    while pos < len(text):
        end = min(pos + chunk_size, len(text))

        if end < len(text):
            # Try to find a good break point within the chunk window
            break_at = _find_break(text[pos:], chunk_size)
            end = pos + break_at

        chunk_text = text[pos:end].strip()
        if chunk_text:
            start_line = text[:pos].count("\n") + 1
            chunks.append(Chunk(
                text=chunk_text,
                source_path=path,
                source_title=title,
                chunk_index=index,
                start_line=start_line,
                source_type=source_type,
            ))
            index += 1

        if end >= len(text):
            break

        pos = max(end - overlap, pos + 1)

    return chunks
