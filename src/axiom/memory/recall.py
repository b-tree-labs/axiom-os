# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Recall index + result shapes for ``CompositionService.recall()``
(ADR-087 D5, over the ADR-088 rag-memory corpus).

``RecallIndex`` maintains the per-principal projection inside an
existing RAG store (SQLite FTS + optional vector, zero-daemon per
ADR-087 D4) and runs the hybrid query: dense and sparse rankings are
fetched independently and fused with the platform's reciprocal-rank
fusion. The index is a rebuildable read-side projection — it has no
write path into the ledger, and recall *results* are always resolved
back through ``CompositionService.read()`` so access checks, signature
verification, and tombstone exclusion hold even against a stale index.

Degradation: with no embedding provider, recall runs FTS-only and says
so (``RecallResult.degraded``) — an embedder outage never breaks recall.

Scoring applies the RPE ``recency_bias`` parameter (resolved from the
intent's plan when not given explicitly). RPE has no ``salience``
parameter yet — tracked as an open question in
``docs/working/cross-mem-p1-open-questions.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

from axiom.memory.fragment import MemoryFragment
from axiom.memory.recall_projection import (
    fragment_to_recall_chunk,
    recall_corpus_for,
    should_recall_project,
)
from axiom.rag.rrf import reciprocal_rank_fusion

_MEMORY_PATH_PREFIX = "memory://"

# Candidate over-fetch multiplier: cognitive-type/time filters apply
# after retrieval in P1 (the per-principal corpus is the SQL-side
# pre-filter), so fetch a wider net than top-k before filtering.
_CANDIDATE_FACTOR = 4


def _default_embedder(texts: list[str]) -> list[list[float]] | None:
    from axiom.rag.embeddings import embed_texts

    return embed_texts(texts)


def _fts_or_query(query: str) -> str:
    """Rewrite a natural-language query into an OR-of-terms FTS5 query.

    FTS5 MATCH implicitly ANDs terms, so a conversational paraphrase
    ("what kind of coffee does she drink") matches nothing unless every
    word appears in one chunk — the known AND-recall-collapse failure
    mode. OR the alphanumeric terms instead and let bm25 rank by the
    informative ones.
    """
    terms = [t for t in query.split() if t.isalnum()]
    if not terms:
        return query
    return " OR ".join(terms)


@dataclass(frozen=True)
class RecallResult:
    """What ``recall()`` returns: ranked fragments + how they were found."""

    fragments: list[MemoryFragment]
    scores: dict[str, float]
    degraded: bool
    corpus: str
    query: str


@dataclass
class RecallIndex:
    """Projection maintainer + hybrid query runner for one RAG store.

    ``embedder`` is any ``list[str] -> list[list[float]] | None``;
    ``None`` (or a provider returning/raising failure) degrades to
    FTS-only. The default is the platform embedding fallback chain.
    """

    store: Any
    embedder: Callable[[list[str]], list[list[float]] | None] | None = field(
        default=_default_embedder,
    )

    # ---- projection maintenance -------------------------------------------

    def index_fragment(self, fragment: MemoryFragment) -> bool:
        """Project one fragment into its principal's rag-memory corpus.

        Returns False (no-op) for non-projectable types (vault). Uses
        the store's per-path replace semantics, so re-indexing the same
        fragment is idempotent.
        """
        if not should_recall_project(fragment):
            return False
        chunk = fragment_to_recall_chunk(fragment)
        embeddings = self._embed([chunk.chunk_text])
        self.store.upsert_chunks(
            [chunk.to_rag_chunk()],
            embeddings=embeddings,
            corpus=chunk.corpus,
            owner=chunk.principal_id,
            cognitive_type=chunk.cognitive_type,
            fragment_ref=chunk.fragment_ref,
        )
        return True

    def evict(self, fragment_id: str, principal_id: str) -> None:
        """Remove a fragment's chunk from its principal's corpus."""
        self.store.delete_document(
            f"{_MEMORY_PATH_PREFIX}{fragment_id}",
            corpus=recall_corpus_for(principal_id),
        )

    def rebuild(self, composition: Any, *, principal: str) -> int:
        """Drop and re-project a principal's corpus from the ledger.

        The projection is disposable (ADR-088 §5): delete-and-rebuild
        is always safe. Returns the number of fragments projected.
        """
        from axiom.memory.fragment import fragment_from_dict

        corpus = recall_corpus_for(principal)
        self.store.delete_corpus(corpus)
        count = 0
        registry = composition.artifact_registry
        backend = getattr(registry, "_backend", None)
        if hasattr(backend, "find_fragments"):
            artifacts = backend.find_fragments(principal_id=principal)
        else:
            artifacts = [
                a for a in registry.list(kind="fragment")
                if (a.data or {}).get("provenance", {}).get("principal_id")
                == principal
            ]
        seen: set[str] = set()
        for artifact in artifacts:
            if artifact.name in seen:
                continue
            seen.add(artifact.name)
            fragment = fragment_from_dict(artifact.data)
            if self.index_fragment(fragment):
                count += 1
        return count

    # ---- hybrid query ------------------------------------------------------

    def search(
        self, query: str, *, principal: str, limit: int
    ) -> tuple[list[str], dict[str, float], bool]:
        """Hybrid dense+sparse → RRF over the principal's corpus.

        Returns ``(fragment_ids best-first, rrf score by id, degraded)``.
        """
        corpus = recall_corpus_for(principal)
        rankings: list[list[str]] = []

        query_embedding = None
        embeddings = self._embed([query])
        degraded = embeddings is None
        if embeddings:
            query_embedding = embeddings[0]
            dense = self.store.search(
                query_embedding=query_embedding,
                query_text="",
                corpora=[corpus],
                limit=limit,
            )
            ranking = [
                r.source_path[len(_MEMORY_PATH_PREFIX):]
                for r in dense
                if r.source_path.startswith(_MEMORY_PATH_PREFIX)
            ]
            if ranking:
                rankings.append(ranking)

        sparse = self.store.search(
            query_embedding=None,
            query_text=_fts_or_query(query),
            corpora=[corpus],
            limit=limit,
        )
        sparse_ranking = [
            r.source_path[len(_MEMORY_PATH_PREFIX):]
            for r in sparse
            if r.source_path.startswith(_MEMORY_PATH_PREFIX)
        ]
        if sparse_ranking:
            rankings.append(sparse_ranking)

        fused = reciprocal_rank_fusion(rankings, limit=limit)
        ids = [str(f.doc_id) for f in fused]
        scores = {str(f.doc_id): f.score for f in fused}
        return ids, scores, degraded

    def _embed(self, texts: list[str]) -> list[list[float]] | None:
        if self.embedder is None:
            return None
        try:
            return self.embedder(texts)
        except Exception:
            # Embedding outages degrade recall to FTS-only; they never
            # break it (PRD F2 / build item 3).
            return None


def _fragment_time(fragment: MemoryFragment) -> str:
    event_time = fragment.content.get("event_time")
    if isinstance(event_time, str) and event_time:
        return event_time
    return fragment.provenance.timestamp


def _recency_score(fragment: MemoryFragment, *, now: datetime) -> float:
    """1/(1 + age_days) — monotone in recency, bounded (0, 1]."""
    try:
        ts = datetime.fromisoformat(_fragment_time(fragment))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
    except ValueError:
        return 0.0
    age_days = max((now - ts).total_seconds(), 0.0) / 86400.0
    return 1.0 / (1.0 + age_days)


def resolve_recency_bias(intent: str, explicit: float | None) -> float:
    """Explicit override wins; otherwise the RPE plan's parameter."""
    if explicit is not None:
        return explicit
    try:
        from axiom.rag.rpe import build_plan

        plan = build_plan("recall", intent, {})
        return float(plan.params.get("recency_bias", 0.0))
    except Exception:
        # Unknown intent or plan failure never breaks recall — it just
        # loses the recency component.
        return 0.0


def rank_fragments(
    fragments: list[MemoryFragment],
    rrf_scores: dict[str, float],
    *,
    recency_bias: float,
    limit: int,
    now: datetime | None = None,
) -> tuple[list[MemoryFragment], dict[str, float]]:
    """RRF score + recency component (ADR-087 D5 / RPE recency_bias)."""
    resolved_now = now or datetime.now(UTC)
    combined = {
        f.id: rrf_scores.get(f.id, 0.0)
        + recency_bias * _recency_score(f, now=resolved_now)
        for f in fragments
    }
    ordered = sorted(fragments, key=lambda f: -combined[f.id])[:limit]
    return ordered, {f.id: combined[f.id] for f in ordered}


__all__ = [
    "RecallIndex",
    "RecallResult",
    "rank_fragments",
    "resolve_recency_bias",
]
