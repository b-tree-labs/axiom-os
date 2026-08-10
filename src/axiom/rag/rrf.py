# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Reciprocal Rank Fusion for hybrid retrieval.

RRF is the standard way to fuse multiple ranked lists (vector, BM25, text
rank, federated peer results) into a single combined ranking. It requires
no score calibration across rankings — only ordinal position — which is
why it holds up as the first-stage fusion in frontier RAG pipelines.

Formula:
    RRF(d) = Σ_i 1 / (k + rank_i(d))

where rank_i(d) is the 1-based position of doc d in ranking i (omitted
if d is absent from that ranking) and k = 60 is the standard constant.

Reference: Cormack, Clarke, Buettcher. "Reciprocal Rank Fusion outperforms
Condorcet and individual Rank Learning Methods." SIGIR 2009.
"""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class FusedResult:
    """A single fused ranking entry."""

    doc_id: Hashable
    score: float
    rank: int  # 1-based position in the fused ranking


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[Hashable]],
    k: int = 60,
    limit: int | None = None,
) -> list[FusedResult]:
    """Fuse multiple rankings into one ordered list via RRF.

    Args:
        rankings: Sequence of ranked lists; each inner list is a ranking of
            doc_ids best-first. A doc_id may appear in some, all, or none
            of the rankings.
        k: Standard RRF constant (default 60).
        limit: If set, truncate the fused list to this many entries.

    Returns:
        ``FusedResult`` entries sorted by descending score, then by first-
        appearance order to break ties deterministically.
    """
    if k <= 0:
        raise ValueError(f"k must be positive; got {k}")

    scores: dict[Hashable, float] = {}
    first_seen: dict[Hashable, int] = {}
    counter = 0
    for ranking in rankings:
        for position, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + position)
            if doc_id not in first_seen:
                first_seen[doc_id] = counter
                counter += 1

    ordered = sorted(
        scores.items(),
        key=lambda kv: (-kv[1], first_seen[kv[0]]),
    )
    if limit is not None:
        ordered = ordered[:limit]

    return [
        FusedResult(doc_id=doc_id, score=score, rank=idx + 1)
        for idx, (doc_id, score) in enumerate(ordered)
    ]
