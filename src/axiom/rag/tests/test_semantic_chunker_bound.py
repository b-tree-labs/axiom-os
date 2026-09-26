# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""semantic chunker — hard size bound (#21).

Regression: boundary-less docs (CSVs, console logs, single-line dumps) had
no \\n\\n to split on and passed through as one multi-MB chunk (observed
3.3M chars), overflowing the serving model context at retrieval.
"""
from __future__ import annotations

from axiom.rag.semantic_chunker import chunk_semantic

MAX = 2000


def test_boundaryless_giant_doc_is_bounded():
    chunks = chunk_semantic("x" * 500_000, "logs/dump.txt", max_chunk_size=MAX)
    assert chunks
    assert max(len(c.text) for c in chunks) <= MAX


def test_single_newline_logs_are_bounded():
    log = "\n".join("2021-01-04 status flow=17.8 lps" for _ in range(20_000))
    chunks = chunk_semantic(log, "logs/console.txt", max_chunk_size=MAX)
    assert max(len(c.text) for c in chunks) <= MAX


def test_normal_prose_still_chunks_semantically():
    text = "\n\n".join(f"## Section {i}\n\nBody paragraph {i}." for i in range(5))
    chunks = chunk_semantic(text, "doc.md", max_chunk_size=MAX)
    assert chunks
    assert max(len(c.text) for c in chunks) <= MAX
