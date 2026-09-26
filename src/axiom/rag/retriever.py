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

from axiom.rag.fts_query import fts_terms
from axiom.rag.hybrid import DEFAULT_VALUE_INTENT_TERMS, RerankWeights, rerank_score
from axiom.rag.rrf import FusedResult, reciprocal_rank_fusion
from axiom.rag.store import SearchResult

_DEFAULT_RERANK_WEIGHTS = RerankWeights()

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
    site: str | None = None
    """The tenant asking (ADR-025 §1), resolved from the credential — never
    from anything the caller says.

    ``None`` means unconstrained, which is the internal/administrative path and
    the pre-tenancy behaviour. When it is set, a chunk is visible only if it
    belongs to that tenant or is explicitly shared: an **unattributed** chunk is
    denied, because absence must not read as "everyone's" — otherwise every
    chunk indexed before tenancy existed becomes visible to every tenant."""


#: A chunk explicitly readable by every tenant — platform documentation, shared
#: reference material. It is a value a chunk must *carry*, never the absence of
#: one, so that un-migrated content fails closed rather than open.
SHARED_SITE = "shared"


def _principal_context(principal: str | None) -> str | None:
    """The context/site half of a ``@name:context`` handle, or None."""
    if not principal:
        return None
    handle = principal[1:] if principal.startswith("@") else principal
    _, sep, context = handle.partition(":")
    return (context.strip() or None) if sep else None


def access_context_for_principal(
    principal: str | None, *, site_scoping: bool = False
) -> AccessContext:
    """Resolve an :class:`AccessContext` for a calling principal.

    The read-side counterpart to the ingest tenancy grant. Today it returns the
    FAIL-CLOSED baseline (``public`` tier, ``unclassified`` only): per-principal
    tier/classification ELEVATION is the deferred policy engine (see
    :class:`AccessContext`), so until it lands every principal reads at the safe
    floor — the correct default, not a regression. This is still a real gate: a
    chunk that carries a higher tier or a classification is now filtered out,
    where before (``access_context=None``) it leaked through unfiltered.

    ``site_scoping`` (default off) derives the tenant from the principal's
    ``@name:context`` handle. It is off by default because chunks have no
    ``site`` column yet: with an unattributed corpus a set ``ctx.site`` would
    deny every chunk (``_permits`` fails closed on unattributed content). It
    becomes safe to enable once ingest attributes chunks to a site.

    Routing every serving surface through this one seam means that when the
    policy engine and site attribution land, access differentiation wires in
    ONE place instead of at each call site.
    """
    site = _principal_context(principal) if site_scoping else None
    return AccessContext(site=site)


@dataclass(frozen=True)
class RetrievedChunk:
    """One chunk surfaced to the model, with citation + provenance metadata."""

    citation_key: str  # "C1", "C2", ... stable within one retrieve() call
    rank: int  # 1-based final position
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
    site: str | None = None


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


def _rerank_fused(
    query_text: str,
    fused: list[FusedResult],
    by_key: dict[tuple[str, int, str], SearchResult],
    weights: RerankWeights,
) -> list[FusedResult]:
    """Reorder RRF-fused results by the shared heuristic rerank score
    (:func:`axiom.rag.hybrid.rerank_score`). Stable — ties keep the fused order —
    and a query carrying no content terms is left untouched, so this only ever
    reorders, never drops."""
    qterms = set(fts_terms(query_text))
    if not qterms or not fused:
        return fused
    value_q = bool(qterms & set(DEFAULT_VALUE_INTENT_TERMS))
    max_rrf = max((fr.score for fr in fused), default=0.0)

    def _score(fr: FusedResult) -> float:
        hit = by_key.get(fr.doc_id)  # type: ignore[arg-type]
        if hit is None:
            return 0.0
        return rerank_score(
            query_terms=qterms,
            text=hit.chunk_text,
            corpus=hit.corpus,
            source_path=hit.source_path,
            source_title=hit.source_title,
            rrf=fr.score,
            max_rrf=max_rrf,
            value_query=value_q,
            weights=weights,
        )

    return sorted(fused, key=_score, reverse=True)


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
    site_lookup: Callable[[str], str | None] | None = None,
    rerank: bool = True,
    rerank_weights: RerankWeights | None = None,
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
        site_lookup: same, for the owning tenant. A chunk whose site cannot
            be resolved is denied when the context names a tenant — see
            :class:`AccessContext`.
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

    # 2b. Heuristic rerank. Folds hybrid.py's reranker onto this store-abstracted
    # path (previously it was RRF-only and hybrid.py's rerank ran nowhere): reorder
    # the fused candidates so value-bearing, on-topic chunks beat generic prose,
    # using the SAME signals as the hybrid stack. Access filter + citation keys
    # then run in reranked order.
    if rerank:
        fused = _rerank_fused(
            query_text, fused, by_key, rerank_weights or _DEFAULT_RERANK_WEIGHTS
        )

    # 3. Access filter + citation-key assignment.
    out: list[RetrievedChunk] = []
    for fr in fused:
        if len(out) >= limit:
            break
        hit = by_key[fr.doc_id]  # type: ignore[index]
        path = hit.source_path
        # Honor the chunk's OWN access metadata (projected by the store) when no
        # explicit lookup override is supplied. Before this, the fallback was a
        # blanket "public"/"unclassified", which made the stored access columns
        # inert at read time — access was populated at ingest and never enforced.
        # ``getattr`` keeps a hit shape that carries no access columns (a mock, a
        # store that doesn't project them) on the prior fail-open default.
        tier = tier_lookup(path) if tier_lookup else getattr(hit, "access_tier", "public")
        classification = (
            classification_lookup(path)
            if classification_lookup
            else getattr(hit, "classification", "unclassified")
        )
        nationalities = (
            nationalities_lookup(path)
            if nationalities_lookup
            else getattr(hit, "allowed_nationalities", None)
        )
        # No ``site``/tenant column exists on chunks yet, so site-scoping stays
        # deferred (an override may still supply it); ``ctx.site`` remains None.
        site = site_lookup(path) if site_lookup else None
        if access_context is not None and not _permits(
            access_context, tier, classification, nationalities, site
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
                site=site,
            )
        )
    return out


def owned_corpora(site: str, available: list[str]) -> list[str]:
    """The corpora a tenant **owns** — the ones an offboard may delete.

    Deliberately narrower than what the tenant may *read*. A flowloop facility
    reads general reactor and molten-salt literature that is more pertinent to
    it than the host's own TRIGA material is, but it does not own a word of it:
    removing that tenant must not remove the library. Ownership is the
    namespace; readership is a subscription.
    """
    prefix = f"{site}:"
    return sorted(c for c in available if c == site or c.startswith(prefix))


def corpora_for_tenant(
    site: str | None,
    available: list[str] | None = None,
    *,
    state_dir: str | None = None,
) -> list[str] | None:
    """``tenant_corpora`` with the tenant's subscriptions read from its record.

    The wiring that makes a subscription real: without it the parameter exists
    and nothing ever supplies it, so a facility still cannot reach the library
    it was subscribed to. A tenant with no record reads only what it owns —
    under-serving rather than over-sharing.
    """
    if site is None:
        return None
    from axiom.infra.tenancy import subscriptions_for

    return tenant_corpora(
        site, available, subscriptions=subscriptions_for(site, state_dir=state_dir)
    )


def tenant_corpora(
    site: str | None,
    available: list[str] | None = None,
    subscriptions: list[str] | None = None,
) -> list[str] | None:
    """The corpora a tenant may draw candidates from.

    The second half of scoping retrieval twice (ADR-025 §3), and the half that
    matters for *quality* rather than only for safety. The access filter runs
    after fusion, so on its own it lets one tenant's chunks fill the candidate
    window and crowd another's out — the caller then gets a correct but nearly
    empty answer and no indication why. Pushing the tenant into the corpus
    filter means candidates are drawn from their own material to begin with.

    A tenant reads what it owns, plus the shared corpus, plus anything it
    **subscribes** to. Subscriptions are configuration, not a naming
    convention, because the useful library for a facility is a topical choice:
    general reactor and molten-salt material is more pertinent to a flow loop
    than the host site's own TRIGA corpus is, and the reverse is equally true.

    Returns ``None`` (no filter) for an unconstrained context, which is the
    administrative path.
    """
    if site is None:
        return None
    if available is None:
        return [site, SHARED_SITE, *(subscriptions or [])]
    scoped = owned_corpora(site, available)
    for corpus in (SHARED_SITE, *(subscriptions or [])):
        if corpus in available and corpus not in scoped:
            scoped.append(corpus)
    return scoped


def _permits(
    ctx: AccessContext,
    tier: str,
    classification: str,
    nationalities: tuple[str, ...] | None,
    site: str | None = None,
) -> bool:
    if _TIER_ORDER.get(tier, 99) > _TIER_ORDER.get(ctx.max_access_tier, 0):
        return False
    if classification not in ctx.allowed_classifications:
        return False
    if nationalities is not None and ctx.nationality not in nationalities:
        return False
    if ctx.site is not None and site != ctx.site and site != SHARED_SITE:
        # Fail closed, including when the chunk's tenant is unknown: an
        # unattributed chunk belongs to nobody, which is not the same as
        # belonging to whoever happens to be asking.
        return False
    return True
