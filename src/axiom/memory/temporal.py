# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Bitemporal memory — valid-time + ingestion-time queries (#36).

Every MemoryFragment carries two time axes:
- **Ingestion time** (Provenance.timestamp) — when the system
  learned / wrote the fragment. Already present on the primitive.
- **Valid time** ([valid_time_start, valid_time_end]) — when the
  fragment's claim is/was true in the world. Optional; `None` end
  means "still valid."

The two axes answer different questions:
- "What did the system *believe* at time T?"   → ingestion filter
- "What was *true* at time T (per our records)?" → valid-time filter

Supersedure: when a new fragment replaces an older one, close the
older fragment's valid_time_end (immutable — returns a new frozen
instance, the original is unchanged).

Inspired by Graphiti (Zep, arXiv 2501.13956). Pure functional API.
"""

from __future__ import annotations

import dataclasses

from .fragment import MemoryFragment

# ---------------------------------------------------------------------------
# Valid-time setters (return new fragment, original unchanged)
# ---------------------------------------------------------------------------


def with_valid_time(
    fragment: MemoryFragment,
    start: str,
    end: str | None = None,
) -> MemoryFragment:
    """Return a new fragment with valid_time_start/end set."""
    return dataclasses.replace(
        fragment,
        valid_time_start=start,
        valid_time_end=end,
        # Signing must happen AFTER valid-time is set (sig covers valid-time fields)
        signature=None,
    )


# ---------------------------------------------------------------------------
# Query: fragments valid at a given time
# ---------------------------------------------------------------------------


def _covers(start: str | None, end: str | None, t: str) -> bool:
    """True iff [start, end) covers t. None endpoints mean open-ended."""
    if start is not None and t < start:
        return False
    if end is not None and t >= end:
        return False
    return True


def fragments_valid_at(
    fragments: list[MemoryFragment], timestamp: str
) -> list[MemoryFragment]:
    """Return fragments whose valid-time interval covers `timestamp`.

    A fragment with no valid_time_start and no valid_time_end is
    considered timeless (always valid). String comparison works for
    ISO 8601 UTC timestamps because they're lexicographically sortable.
    """
    return [
        f for f in fragments
        if _covers(f.valid_time_start, f.valid_time_end, timestamp)
    ]


# ---------------------------------------------------------------------------
# Supersedure: close an older fragment's valid-time
# ---------------------------------------------------------------------------


def supersede(
    fragment: MemoryFragment,
    by: MemoryFragment,
) -> MemoryFragment:
    """Return `fragment` with its valid_time_end closed to `by.valid_time_start`.

    `by` must have a valid_time_start that is not earlier than
    `fragment.valid_time_start` — otherwise the supersedure is
    nonsensical (new fact starts earlier than old fact).
    """
    if by.valid_time_start is None:
        raise ValueError("superseding fragment must have a valid_time_start")
    if fragment.valid_time_start is not None and by.valid_time_start < fragment.valid_time_start:
        raise ValueError(
            f"cannot supersede with earlier valid_time_start "
            f"({by.valid_time_start} < {fragment.valid_time_start})"
        )
    return dataclasses.replace(
        fragment,
        valid_time_end=by.valid_time_start,
        signature=None,  # must re-sign after mutation
    )


# ---------------------------------------------------------------------------
# Query: fragments known-at (ingestion time)
# ---------------------------------------------------------------------------


def fragments_known_at(
    fragments: list[MemoryFragment], timestamp: str
) -> list[MemoryFragment]:
    """Return fragments whose ingestion time is <= `timestamp`.

    Answers "what did the system know at time T?" — useful for
    reproducibility replays (grade disputes, syllabus audits,
    research dataset point-in-time reconstructions).
    """
    return [f for f in fragments if f.provenance.timestamp <= timestamp]
