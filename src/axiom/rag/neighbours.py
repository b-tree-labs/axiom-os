# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Pull in the chunks next to a table-like hit when the user wants the whole thing (#1984).

A table or list is split across consecutive chunks of its document, and retrieval returns whichever
chunks rank. Asked for "all runs including 8-10 and 18-20", a chat held the chunk with runs 1-7
and said the rest was cut off; runs 8-10 were in the next chunk.
Expansion needs both a completeness cue in the question and a fragment-like hit, so ordinary
questions retrieve exactly what they did before.

Pure (no database): :func:`axiom.rag.retriever.retrieve` fetches the planned chunks, applying the same corpus and tier
filters as retrieval, and passes them to ``merge_neighbours``.
"""
from __future__ import annotations

import re

_CUE = re.compile(
    r"\b(?:all|every|each|full|complete|entire|whole|rest|remaining|list|table|rows?|runs?\s+\d)\b"
    r"|\b\d+\s*[-–—]\s*\d+\b",
    re.I,
)

# Row-like lines: start with a number, or contain a date.
_ROWLIKE = re.compile(
    r"^\s*\d{1,3}\b.*$|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}\b",
    re.I | re.M,
)


def wants_completeness(question: str | None) -> bool:
    return bool(question and _CUE.search(question))


def fragment_like(text: str | None) -> bool:
    """A chunk that looks like part of a table or list: many short row-like lines, or mostly digits."""
    if not text or not text.strip():
        return False
    lines = [ln for ln in text.splitlines() if ln.strip()]
    rows = sum(1 for ln in lines if _ROWLIKE.search(ln) and len(ln) < 120)
    chars = [c for c in text if not c.isspace()]
    digit_share = sum(c.isdigit() for c in chars) / max(1, len(chars))
    return (rows >= 4 and rows >= 0.5 * len(lines)) or digit_share >= 0.25


_CAPTION = re.compile(r"\bTable\s+[A-Z]?\d+[\d.\-]*", re.I)


def table_start_tail(text: str | None) -> bool:
    """A chunk that ends at a table's caption and column headers, with the rows still to come.

    The rows begin in the next chunk (a chunk ending at "Table 3-2. Dates of Runs / Run No. Starting
    Date" has its rows in the two chunks after it). A caption mention inside running prose,
    followed by more text, is not a table start."""
    if not text or not text.strip():
        return False
    last = None
    for m in _CAPTION.finditer(text):
        last = m
    if last is None:
        return False
    tail = text[last.end():]
    lines = [ln for ln in tail.splitlines() if ln.strip()]
    rowish = sum(1 for ln in lines if _ROWLIKE.search(ln) and len(ln) < 120)
    return len(tail.split()) <= 20 and len(lines) <= 3 and rowish == 0


def plan_neighbours(rows, question: str | None, max_extra: int = 8):
    """[(path, corpus, chunk_index)] to fetch: the chunks either side of fragment-like hits.

    rows are (source_path, title, text, chunk_index, corpus, score). Empty when the question has
    no completeness cue. Never asks for a chunk already present, a negative index, or more than
    max_extra chunks. Every fragment-like hit gets its FOLLOWING chunk before any gets its
    preceding one: a table continues downward, and a budget spent early on one hit's two sides
    must not starve another hit's continuation (found 2026-10-03 replaying the run-table question)."""
    if not wants_completeness(question):
        return []
    have = {(r[0], r[4], r[3]) for r in rows}
    out: list[tuple[str, str, int]] = []
    frags = [(path, corpus, idx) for path, _t, text, idx, corpus, _s in rows
             if idx is not None and fragment_like(text)]
    starts = [(path, corpus, idx) for path, _t, text, idx, corpus, _s in rows
              if idx is not None and table_start_tail(text) and not fragment_like(text)]
    # Following chunks first (a table continues downward), then a table start's second chunk (its
    # rows can run across two), then the preceding chunk of a row-dense hit.
    for hits, step in ((frags + starts, 1), (starts, 2), (frags, -1)):
        for path, corpus, idx in hits:
            key = (path, corpus, idx + step)
            if key[2] >= 0 and key not in have and key not in out:
                out.append(key)
                if len(out) >= max_extra:
                    return out
    return out


def merge_neighbours(rows, fetched):
    """rows with each fetched neighbour placed beside its anchor, the document's own order restored.

    A fetched chunk lands in the group of the hit it continues: next to the anchor, or next to a
    chunk already in that group, so a table's second continuation chunk follows its first instead
    of trailing after every other hit. Within a group chunks run in document order, so a table
    reads top to bottom. Anything already present is not repeated."""
    have = {(r[0], r[4], r[3]) for r in rows}
    extra = [f for f in fetched if (f[0], f[4], f[3]) not in have]
    out = []
    for r in rows:
        group = [r]
        grown = True
        while grown:
            grown = False
            for f in list(extra):
                if f[0] == r[0] and f[4] == r[4] and any(abs(f[3] - g[3]) == 1 for g in group):
                    group.append(f)
                    extra.remove(f)
                    grown = True
        out.extend(sorted(group, key=lambda x: x[3]))
    out.extend(extra)  # nothing to stand beside: kept, after the hits
    seen, dedup = set(), []
    for r in out:
        k = (r[0], r[4], r[3])
        if k not in seen:
            seen.add(k)
            dedup.append(r)
    return dedup
