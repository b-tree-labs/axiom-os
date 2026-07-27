# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RAG retriever — orchestrates hybrid search, RRF fusion, access filter.

This is the entry point used by the chat agent (and any other caller that
needs context for grounded generation). Sequence:

    1. Two parallel calls into ``RAGStore.search``:
         a) pure vector (embedding only)
         b) pure text  (BM25 / tsvector only)
    2. RRF fuses the two rankings (frontier-parity approach — no score
       calibration needed).
    3. Optional access filter drops chunks the caller may not see.
       Filtering happens *before* citation-key assignment so keys remain
       dense (C1, C2, ... with no gaps).
    4. Optional cross-encoder rerank (not yet wired — handled in a
       separate layer once ``sentence-transformers`` lands).
    5. Top ``limit`` chunks are returned as ``RetrievedChunk`` with a
       stable ``citation_key`` the downstream prompt template and
       citation postprocessor rely on.

Access-control metadata (``access_tier``, ``classification``,
``allowed_nationalities``) comes from the chunks table columns added by
the T0-1 schema migration. Pre-populated chunks retrieved via
``store.search`` don't carry that metadata yet; callers pass a
``tier_lookup`` callable (typically a DB-join wrapper) so the retriever
can enforce the filter. A later iteration will surface these columns on
``SearchResult`` directly and drop the callable.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from axiom.rag.rrf import reciprocal_rank_fusion
from axiom.rag.store import SearchResult

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

_TIER_ORDER: dict[str, int] = {
    "public": 0,
    "course": 1,
    "institutional": 2,
    "classified": 3,
}


@dataclass(frozen=True)
class AccessContext:
    """Who is asking, what they are allowed to see.

    For T0-1 this is intentionally narrow — the full policy engine
    (π_global, π_u, π_a, π_t) will wrap this later.
    """

    max_access_tier: str = "public"
    allowed_classifications: frozenset[str] = field(
        default_factory=lambda: frozenset({"unclassified"})
    )
    nationality: str | None = None


@dataclass(frozen=True)
class RetrievedChunk:
    """One chunk surfaced to the model, with citation + provenance metadata."""

    citation_key: str          # "C1", "C2", ... stable within one retrieve() call
    rank: int                  # 1-based final position
    source_path: str
    source_title: str
    chunk_text: str
    chunk_index: int
    corpus: str
    similarity: float
    rrf_score: float
    access_tier: str = "public"
    classification: str = "unclassified"
    allowed_nationalities: tuple[str, ...] | None = None


# ---------------------------------------------------------------------------
# Store interface (protocol only — keeps retriever testable)
# ---------------------------------------------------------------------------


class _StoreLike(Protocol):
    def search(
        self,
        query_embedding: list[float] | None = None,
        query_text: str = "",
        corpora: list[str] | None = None,
        limit: int = 5,
        chunking_tier: str | None = None,
        corpus_generation: int | None = None,
    ) -> list[SearchResult]: ...


# ---------------------------------------------------------------------------
# retrieve
# ---------------------------------------------------------------------------


def _chunk_key(r: SearchResult) -> tuple[str, int, str]:
    """Identity under fusion: same path+chunk_index+corpus = same chunk."""
    return (r.source_path, r.chunk_index, r.corpus)


def retrieve(
    store: _StoreLike,
    query_text: str,
    query_embedding: list[float] | None,
    corpora: list[str] | None = None,
    limit: int = 8,
    retrieval_breadth: int = 24,
    access_context: AccessContext | None = None,
    tier_lookup: Callable[[str], str] | None = None,
    classification_lookup: Callable[[str], str] | None = None,
    nationalities_lookup: Callable[[str], tuple[str, ...] | None] | None = None,
) -> list[RetrievedChunk]:
    """Return top-``limit`` retrieved chunks for a query.

    Args:
        store: anything implementing the ``_StoreLike`` protocol.
        query_text: natural-language query for BM25/FTS.
        query_embedding: vector for the embedding search (may be None —
            in which case only the text ranking is used).
        corpora: optional corpus filter.
        limit: final list size after RRF + filter.
        retrieval_breadth: number of candidates to pull from each backend
            before fusion. Frontier RAG typically uses 20–50.
        access_context: who is asking. If omitted, no filter is applied.
        tier_lookup: callable returning the access_tier for a given
            source_path. Wired in the production path to a DB join;
            mocked in tests. If omitted, chunks default to 'public'.
        classification_lookup: same, for classification.
        nationalities_lookup: same, for allowed_nationalities (None =
            unrestricted).
    """
    # 1. Gather rankings.
    vector_hits: list[SearchResult] = []
    text_hits: list[SearchResult] = []
    if query_embedding is not None:
        vector_hits = store.search(
            query_embedding=query_embedding,
            query_text="",
            corpora=corpora,
            limit=retrieval_breadth,
        )
    if query_text.strip():
        text_hits = store.search(
            query_embedding=None,
            query_text=query_text,
            corpora=corpora,
            limit=retrieval_breadth,
        )

    # 2. Build identity→result map + list-of-rankings for RRF.
    by_key: dict[tuple[str, int, str], SearchResult] = {}
    for hit in list(vector_hits) + list(text_hits):
        by_key.setdefault(_chunk_key(hit), hit)

    rankings = [
        [_chunk_key(r) for r in vector_hits],
        [_chunk_key(r) for r in text_hits],
    ]
    fused = reciprocal_rank_fusion(rankings, k=60, limit=None)

    # 3. Access filter + citation-key assignment.
    out: list[RetrievedChunk] = []
    for fr in fused:
        if len(out) >= limit:
            break
        hit = by_key[fr.doc_id]  # type: ignore[index]
        path = hit.source_path
        tier = tier_lookup(path) if tier_lookup else "public"
        classification = (
            classification_lookup(path) if classification_lookup else "unclassified"
        )
        nationalities = (
            nationalities_lookup(path) if nationalities_lookup else None
        )
        if access_context is not None and not _permits(
            access_context, tier, classification, nationalities
        ):
            continue
        out.append(
            RetrievedChunk(
                citation_key=f"C{len(out) + 1}",
                rank=len(out) + 1,
                source_path=hit.source_path,
                source_title=hit.source_title,
                chunk_text=hit.chunk_text,
                chunk_index=hit.chunk_index,
                corpus=hit.corpus,
                similarity=hit.similarity,
                rrf_score=fr.score,
                access_tier=tier,
                classification=classification,
                allowed_nationalities=nationalities,
            )
        )
    return out


def _permits(
    ctx: AccessContext,
    tier: str,
    classification: str,
    nationalities: tuple[str, ...] | None,
) -> bool:
    if _TIER_ORDER.get(tier, 99) > _TIER_ORDER.get(ctx.max_access_tier, 0):
        return False
    if classification not in ctx.allowed_classifications:
        return False
    if nationalities is not None and ctx.nationality not in nationalities:
        return False
    return True
