# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Semantic document chunker — splits at structural boundaries.

Detects headings, tables, code blocks, regulatory sections, and
paragraph clusters to produce semantically coherent chunks.
Falls back to fixed-size chunking for unstructured text.

Usage::

    from axiom.rag.semantic_chunker import chunk_semantic
    chunks = chunk_semantic(text, "doc.md")
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .chunker import Chunk


@dataclass
class SemanticBoundary:
    """A structural boundary detected in document text."""

    offset: int
    boundary_type: str  # "heading", "table", "code_block", "section", "paragraph"
    level: int = 0  # heading level 1-6, or 0 for others
    metadata: dict | None = None


def detect_boundaries(text: str) -> list[SemanticBoundary]:
    """Detect structural boundaries in document text.

    Returns boundaries sorted by offset.
    """
    boundaries: list[SemanticBoundary] = []

    lines = text.split("\n")
    offset = 0

    in_table = False
    table_start = 0
    in_code = False
    code_start = 0

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Code block boundaries (``` fences)
        if stripped.startswith("```"):
            if not in_code:
                in_code = True
                code_start = offset
            else:
                in_code = False
                boundaries.append(
                    SemanticBoundary(
                        offset=code_start,
                        boundary_type="code_block",
                        metadata={"end_offset": offset + len(line)},
                    )
                )
            offset += len(line) + 1
            continue

        if in_code:
            offset += len(line) + 1
            continue

        # Heading boundaries (# Markdown headings)
        heading_match = re.match(r"^(#{1,6})\s+(.+)", stripped)
        if heading_match:
            level = len(heading_match.group(1))
            boundaries.append(
                SemanticBoundary(
                    offset=offset,
                    boundary_type="heading",
                    level=level,
                    metadata={"text": heading_match.group(2).strip()},
                )
            )
            offset += len(line) + 1
            continue

        # Regulatory section boundaries (§ XX.XX or "Section XX")
        section_match = re.match(r"^[§$]\s*\d+\.\d+", stripped)
        if section_match:
            boundaries.append(
                SemanticBoundary(
                    offset=offset,
                    boundary_type="section",
                    metadata={"text": stripped[:60]},
                )
            )
            offset += len(line) + 1
            continue

        # Table boundaries (lines with | delimiters)
        if "|" in stripped and stripped.startswith("|"):
            if not in_table:
                in_table = True
                table_start = offset
        elif in_table:
            in_table = False
            boundaries.append(
                SemanticBoundary(
                    offset=table_start,
                    boundary_type="table",
                    metadata={"end_offset": offset},
                )
            )

        # Double newline = paragraph boundary
        if stripped == "" and i > 0:
            prev = lines[i - 1].strip() if i > 0 else ""
            if prev == "":
                boundaries.append(
                    SemanticBoundary(
                        offset=offset,
                        boundary_type="paragraph",
                    )
                )

        offset += len(line) + 1

    # Close any open table
    if in_table:
        boundaries.append(
            SemanticBoundary(
                offset=table_start,
                boundary_type="table",
                metadata={"end_offset": offset},
            )
        )

    return sorted(boundaries, key=lambda b: b.offset)


def chunk_semantic(
    text: str,
    path: str,
    boundaries: list[SemanticBoundary] | None = None,
    min_chunk_size: int = 200,
    max_chunk_size: int = 2000,
    overlap_sentences: int = 1,
) -> list[Chunk]:
    """Split text at semantic boundaries.

    If no boundaries provided, detects them automatically.
    Falls back to simple paragraph splitting for very short text.

    Args:
        text: Full document text
        path: Source file path
        boundaries: Pre-detected boundaries (or None to auto-detect)
        min_chunk_size: Minimum chunk size in characters
        max_chunk_size: Maximum chunk size in characters
        overlap_sentences: Number of sentences to overlap between chunks

    Returns:
        List of Chunk objects covering the full document
    """
    if not text or not text.strip():
        return []

    if len(text) < min_chunk_size:
        return [
            Chunk(
                text=text.strip(),
                source_path=path,
                source_title=_extract_title(text),
                chunk_index=0,
                start_line=1,
                source_type=_infer_type(path),
            )
        ]

    if boundaries is None:
        boundaries = detect_boundaries(text)

    title = _extract_title(text)
    source_type = _infer_type(path)

    # Build sections from boundaries
    sections = _split_at_boundaries(text, boundaries)

    # Merge small sections, split large ones
    chunks: list[Chunk] = []
    buffer = ""
    buffer_start_line = 1

    for section_text, start_line in sections:
        if not section_text.strip():
            continue

        # If adding this section would exceed max, flush buffer first
        if buffer and len(buffer) + len(section_text) > max_chunk_size:
            chunks.append(
                Chunk(
                    text=buffer.strip(),
                    source_path=path,
                    source_title=title,
                    chunk_index=len(chunks),
                    start_line=buffer_start_line,
                    source_type=source_type,
                )
            )
            # Overlap: keep last sentence(s) from buffer
            overlap = _last_sentences(buffer, overlap_sentences)
            buffer = overlap + "\n\n" if overlap else ""
            buffer_start_line = start_line

        if not buffer:
            buffer_start_line = start_line

        buffer += section_text + "\n\n"

        # If this single section exceeds max, split it
        if len(buffer) > max_chunk_size:
            sub_chunks = _split_large_section(
                buffer,
                path,
                title,
                source_type,
                len(chunks),
                buffer_start_line,
                max_chunk_size,
                overlap_sentences,
            )
            chunks.extend(sub_chunks)
            buffer = ""

    # Flush remaining buffer
    if buffer.strip():
        # If buffer is too small, merge with last chunk
        if len(buffer.strip()) < min_chunk_size and chunks:
            last = chunks[-1]
            chunks[-1] = Chunk(
                text=last.text + "\n\n" + buffer.strip(),
                source_path=path,
                source_title=title,
                chunk_index=last.chunk_index,
                start_line=last.start_line,
                source_type=source_type,
            )
        else:
            chunks.append(
                Chunk(
                    text=buffer.strip(),
                    source_path=path,
                    source_title=title,
                    chunk_index=len(chunks),
                    start_line=buffer_start_line,
                    source_type=source_type,
                )
            )

    # Reindex
    for i, c in enumerate(chunks):
        chunks[i] = Chunk(
            text=c.text,
            source_path=c.source_path,
            source_title=c.source_title,
            chunk_index=i,
            start_line=c.start_line,
            source_type=c.source_type,
        )

    return chunks


def _split_at_boundaries(
    text: str,
    boundaries: list[SemanticBoundary],
) -> list[tuple[str, int]]:
    """Split text into sections at boundary offsets.

    Returns list of (section_text, start_line) tuples.
    """
    if not boundaries:
        return [(text, 1)]

    # Use heading and section boundaries as split points
    split_offsets = [0]
    for b in boundaries:
        if b.boundary_type in ("heading", "section"):
            if b.offset > 0:
                split_offsets.append(b.offset)
    split_offsets.append(len(text))

    # Deduplicate and sort
    split_offsets = sorted(set(split_offsets))

    sections = []
    for i in range(len(split_offsets) - 1):
        start = split_offsets[i]
        end = split_offsets[i + 1]
        section_text = text[start:end]
        start_line = text[:start].count("\n") + 1
        sections.append((section_text, start_line))

    return sections


def _split_large_section(
    text: str,
    path: str,
    title: str,
    source_type: str,
    start_index: int,
    start_line: int,
    max_size: int,
    overlap_sentences: int,
) -> list[Chunk]:
    """Split an oversized section into smaller chunks at paragraph boundaries.

    Any single paragraph that itself exceeds ``max_size`` is hard-split into
    character windows. Without this, boundary-less documents (CSVs, console
    logs, single-line dumps) have no ``\\n\\n`` to split on and pass through as
    one multi-MB chunk — which then overflows the serving model's context
    window at retrieval time (observed: chunks up to 3.3M chars).
    """
    chunks = []
    paragraphs: list[str] = []
    for para in re.split(r"\n\n+", text):
        if len(para) <= max_size:
            paragraphs.append(para)
        else:
            # Last-resort hard bound: no semantic/paragraph boundary exists.
            for i in range(0, len(para), max_size):
                paragraphs.append(para[i:i + max_size])
    buffer = ""
    buf_line = start_line

    for para in paragraphs:
        if not para.strip():
            continue
        if buffer and len(buffer) + len(para) > max_size:
            chunks.append(
                Chunk(
                    text=buffer.strip(),
                    source_path=path,
                    source_title=title,
                    chunk_index=start_index + len(chunks),
                    start_line=buf_line,
                    source_type=source_type,
                )
            )
            overlap = _last_sentences(buffer, overlap_sentences)
            buffer = overlap + "\n\n" if overlap else ""
            buf_line = start_line + text[: text.find(para)].count("\n")

        buffer += para + "\n\n"

    if buffer.strip():
        chunks.append(
            Chunk(
                text=buffer.strip(),
                source_path=path,
                source_title=title,
                chunk_index=start_index + len(chunks),
                start_line=buf_line,
                source_type=source_type,
            )
        )

    return chunks


def _last_sentences(text: str, n: int) -> str:
    """Extract the last N sentences from text."""
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    if len(sentences) <= n:
        return ""
    return " ".join(sentences[-n:])


def _extract_title(text: str) -> str:
    """Extract first heading as title."""
    for line in text.splitlines()[:20]:
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _infer_type(path: str) -> str:
    """Infer source type from file extension."""
    if path.endswith(".md"):
        return "markdown"
    if path.endswith(".pdf"):
        return "pdf"
    if path.endswith(".txt"):
        return "text"
    return "markdown"
