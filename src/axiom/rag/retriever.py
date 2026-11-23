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

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Protocol

from axiom.rag.hybrid import RerankWeights, rerank_score
from axiom.rag.ranking import RankingProfile, active_profile
from axiom.rag.rrf import FusedResult, reciprocal_rank_fusion
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
    clearance: str | None = None
    """The person's export-control clearance (``governance.classification``).

    A chunk whose ``classification`` is an export-control label (``public``,
    ``internal``, ``regulated``, ``controlled``; stamped by the library when a
    person confirms an item's label) is returned only when the label is at or
    below this clearance (MVP-06-A1). ``None`` is the fail-closed floor,
    ``public``. Legacy classifications keep the ``allowed_classifications``
    check."""
    principal: str | None = None
    """Who is asking, as a handle. Sources with per-item access (:mod:`axiom.rag.source_guards`,
    a Library file shared by name) answer for this person; ``None`` is nobody in particular,
    who reads only what a source shares with everyone at its site."""
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


class _Unfiltered:
    """The type of :data:`UNFILTERED`; never instantiated by callers."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UNFILTERED"


#: Retrieve with **no** access filter at all.
#:
#: The one legitimate use is a store that ran the filter itself — a remote peer
#: whose server-side hybrid search already applied its own policy, where a
#: second local filter would second-guess a decision made with information this
#: node does not have.
#:
#: It is a named sentinel rather than ``None`` on purpose. ``None`` is what a
#: variable holds when something upstream did not happen, and a filter that
#: switches itself off in exactly that case is the failure mode this exists to
#: prevent. Passing this is a claim, greppable in review; passing ``None`` is
#: not a claim, and is treated as the fail-closed baseline.
UNFILTERED = _Unfiltered()


class _Unset:
    """Distinguishes "argument omitted" from an explicitly passed value."""

    __slots__ = ()


_UNSET = _Unset()


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
    from axiom.governance.classification import clearance_for

    site = _principal_context(principal) if site_scoping else None
    return AccessContext(site=site, clearance=clearance_for(principal).value, principal=(principal or None))


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
    profile: RankingProfile | None = None,
) -> list[FusedResult]:
    """Reorder RRF-fused results by the shared heuristic rerank score
    (:func:`axiom.rag.hybrid.rerank_score`). Stable — ties keep the fused order —
    and a query carrying no content terms is left untouched, so this only ever
    reorders, never drops. ``profile`` carries the site's declarations (its
    authority corpus, value words, code tokens and document kinds); the default
    profile declares none."""
    profile = profile or RankingProfile()
    qterms = set(profile.terms(query_text))
    if not qterms or not fused:
        return fused
    value_q = bool(qterms & set(profile.value_intent_terms))
    primary_q = profile.wants_primary_document(query_text)
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
            authority_corpus=profile.authority_corpus,
            authority_pattern=profile.authority_pattern,
            extra_code_pattern=profile.extra_code_pattern,
            developer=profile.is_developer_file(hit.source_path),
            draft=profile.is_draft(hit.source_path, hit.source_title),
            primary=primary_q and profile.is_primary_document(hit.source_path, hit.source_title),
        )

    return sorted(fused, key=_score, reverse=True)


def _accepts(fn: Callable, name: str) -> bool:
    """Whether ``fn`` takes the keyword ``name`` (a store that predates it is left alone)."""
    import inspect

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


def _identifier_rankings(
    store: _StoreLike, query_text: str, corpora: list[str] | None, breadth: int, prof: RankingProfile,
    gen_kw: dict[str, int] | None = None,
) -> list[list[SearchResult]]:
    """The text rankings that REQUIRE the question's identifiers: any one of them (with the
    other terms ORed), and all of them together. A broad OR query buries a rare code under the
    generic terms around it, and a scan-bounded ranking then drops it. Fail-soft: a ranking
    that errors is dropped and the others carry the turn."""
    if not prof.entity_rankings or not query_text.strip() or not _accepts(store.search, "tsquery"):
        return []
    from axiom.rag.fts_query import fts_entity_all_tsquery, fts_entity_tsquery, fts_tsquery

    broad = fts_tsquery(query_text)
    anyq = fts_entity_tsquery(query_text, extra_code_re=prof.extra_code_pattern)
    allq = fts_entity_all_tsquery(query_text)
    out: list[list[SearchResult]] = []
    for q in dict.fromkeys(x for x in (anyq, allq) if x and x != broad):
        try:
            hits = store.search(query_embedding=None, query_text=query_text, corpora=corpora,
                                limit=breadth, tsquery=q, **(gen_kw or {}))
        except Exception:  # noqa: BLE001 - fail-soft by contract
            continue
        if isinstance(hits, list) and hits:
            out.append(hits)
    return out


def _name_hits(
    store: _StoreLike, query_text: str, query_embedding: list[float] | None,
    corpora: list[str] | None, prof: RankingProfile, gen_kw: dict[str, int] | None = None,
) -> list[SearchResult]:
    """Chunks of the sources the question names (``RAGStore.name_ranking``), best first.
    Empty unless the profile turns it on and the store can; never raises."""
    fn = getattr(store, "name_ranking", None)
    if not prof.name_ranking or not callable(fn):
        return []
    tsq, max_sources = prof.name_query(query_text)
    if not tsq:
        return []
    try:
        hits = fn(tsq, corpora, max_sources=max_sources, limit=prof.name_chunks,
                  exclude_path_re=prof.developer_path_re, query_embedding=query_embedding,
                  names_table=prof.names_table, **(gen_kw or {}))
    except Exception:  # noqa: BLE001 - a bonus ranking never costs the turn its passages
        return []
    return list(hits) if isinstance(hits, list) else []


def _dedup_by_prefix(
    fused: list[FusedResult], by_key: dict[tuple[str, int, str], SearchResult], chars: int
) -> list[FusedResult]:
    """Keep the better-ranked of two chunks whose text starts the same (one passage chunked
    twice)."""
    seen: set[str] = set()
    out: list[FusedResult] = []
    for fr in fused:
        prefix = (by_key[fr.doc_id].chunk_text or "")[:chars]  # type: ignore[index]
        if prefix in seen:
            continue
        seen.add(prefix)
        out.append(fr)
    return out


def _reserve_named(fused: list[FusedResult], name_hits: list[SearchResult], n: int) -> list[FusedResult]:
    """The candidates for the reserved places: the named sources' chunks, best first.

    :func:`retrieve` walks these ahead of the ordinary ranking and accepts at most ``n`` of
    them; all are offered so that one the access filter drops is replaced by the next. A named
    chunk not accepted here keeps its ordinary place in the ranking after them."""
    if not name_hits or n <= 0:
        return []
    score = {fr.doc_id: fr.score for fr in fused}
    lead, seen = [], set()
    for h in name_hits:
        key = _chunk_key(h)
        if key in seen:
            continue
        seen.add(key)
        lead.append(FusedResult(doc_id=key, score=score.get(key, 0.0), rank=0))
    return lead


#: Chunks of the already-cited sources that lead a completeness follow-up.
CITED_CHUNKS = 4


def _vector_corpora(corpora: Sequence[str] | None, keyword_only: Sequence[str]) -> list[str] | None:
    """The corpora a vector may search: ``corpora`` (``None``: every corpus) without the keyword-only ones."""
    if not keyword_only:
        return list(corpora) if corpora is not None else None
    from axiom.rag.store import ALL_CORPORA

    excluded = set(keyword_only)
    return [c for c in (corpora if corpora is not None else ALL_CORPORA) if c not in excluded]


def search_with_fallback(
    store: _StoreLike, embedded, query_text: str, *, corpora: Sequence[str] | None = None, limit: int = 5, **kw
) -> list[SearchResult]:
    """``store.search`` for a path that calls it itself, with ``embed_query``'s per-corpus fallback.

    With no keyword-only corpus this is one ``store.search`` as before. Otherwise
    the corpora that may use the vector are searched hybrid, the keyword-only
    ones by text, and the two rankings fuse through RRF, so only the corpus
    whose recorded model differs loses its vector ranking (P0.2).
    """
    vector = getattr(embedded, "vector", None)
    keyword_only = tuple(getattr(embedded, "keyword_only", ()) or ())
    if not keyword_only or vector is None:
        return store.search(query_embedding=vector, query_text=query_text,
                            corpora=list(corpora) if corpora is not None else None, limit=limit, **kw)
    hybrid = store.search(query_embedding=vector, query_text=query_text,
                          corpora=_vector_corpora(corpora, keyword_only), limit=limit, **kw)
    lexical = store.search(query_embedding=None, query_text=query_text, corpora=list(keyword_only),
                           limit=limit, **kw)
    by_key = {_chunk_key(h): h for h in [*hybrid, *lexical]}
    fused = reciprocal_rank_fusion(
        [[_chunk_key(h) for h in hybrid], [_chunk_key(h) for h in lexical]], limit=limit
    )
    return [by_key[f.doc_id] for f in fused]


def _expand(store, out, query_text, expand_for, cited, cited_suffixes, corpora, admit, gen_kw=None):
    """``out`` with the cited sources' best chunks first and table neighbours beside their
    anchors. Each added chunk goes through ``admit`` (the access filter); a store without the
    methods, or one that fails, leaves ``out`` as it was."""
    from axiom.rag import neighbours

    have = {(c.source_path, c.corpus, c.chunk_index) for c in out}
    paths = list(cited or ())
    find_paths = getattr(store, "paths_ending_with", None)
    if cited_suffixes and callable(find_paths):
        try:
            paths += [p for p in find_paths(list(cited_suffixes), corpora) if p not in paths]
        except Exception:  # noqa: BLE001 - a bonus never costs the turn its passages
            pass
    best_in = getattr(store, "best_chunks_in", None)
    if paths and callable(best_in):
        try:
            found = best_in(paths, query_text, corpora, limit=CITED_CHUNKS, **(gen_kw or {}))
        except Exception:  # noqa: BLE001
            found = []
        lead = []
        for hit in found if isinstance(found, list) else []:
            key = (hit.source_path, hit.corpus, hit.chunk_index)
            chunk = admit(hit, 0.0) if key not in have else None
            if chunk is not None:
                lead.append(chunk)
                have.add(key)
        out = lead + out

    at = getattr(store, "chunks_at", None)
    rows = [(c.source_path, c.source_title, c.chunk_text, c.chunk_index, c.corpus, c.rrf_score) for c in out]
    plan = neighbours.plan_neighbours(rows, expand_for)
    if not plan or not callable(at):
        return out
    try:
        fetched = at(plan, corpora, **(gen_kw or {}))
    except Exception:  # noqa: BLE001
        return out
    admitted = {}
    for hit in fetched if isinstance(fetched, list) else []:
        chunk = admit(hit, 0.0)
        if chunk is not None:
            admitted[(chunk.source_path, chunk.corpus, chunk.chunk_index)] = chunk
    by_row = {(c.source_path, c.corpus, c.chunk_index): c for c in out} | admitted
    merged = neighbours.merge_neighbours(
        rows, [(c.source_path, c.source_title, c.chunk_text, c.chunk_index, c.corpus, 0.0) for c in admitted.values()]
    )
    return [by_row[(r[0], r[4], r[3])] for r in merged]


def retrieve(
    store: _StoreLike,
    query_text: str,
    query_embedding: list[float] | None,
    corpora: list[str] | None = None,
    limit: int = 8,
    retrieval_breadth: int = 24,
    access_context: AccessContext | None | _Unfiltered | _Unset = _UNSET,
    tier_lookup: Callable[[str], str] | None = None,
    classification_lookup: Callable[[str], str] | None = None,
    nationalities_lookup: Callable[[str], tuple[str, ...] | None] | None = None,
    site_lookup: Callable[[str], str | None] | None = None,
    rerank: bool = True,
    rerank_weights: RerankWeights | None = None,
    profile: RankingProfile | None = None,
    expand_for: str | None = None,
    cited: Sequence[str] | None = None,
    cited_suffixes: Sequence[str] | None = None,
    corpus_generation: int | None = None,
    keyword_only_corpora: Sequence[str] = (),
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
        access_context: who is asking. **Omitting it applies the
            fail-closed baseline** (public tier, unclassified only), and so
            does passing ``None`` — a value that is unexpectedly ``None`` must
            narrow the result, never widen it. To retrieve genuinely
            unfiltered, pass :data:`UNFILTERED`, which is a deliberate claim
            rather than an absence.
        tier_lookup: callable returning the access_tier for a given
            source_path. Wired in the production path to a DB join;
            mocked in tests. If omitted, chunks default to 'public'.
        classification_lookup: same, for classification.
        nationalities_lookup: same, for allowed_nationalities (None =
            unrestricted).
        site_lookup: same, for the owning tenant. A chunk whose site cannot
            be resolved is denied when the context names a tenant — see
            :class:`AccessContext`.
        profile: the site's ranking declarations (:mod:`axiom.rag.ranking`).
            ``None`` reads the node's profile (``AXIOM_RAG_RANKING``), so every
            surface on a node ranks the same way; with no profile file, the
            default changes nothing. ``rerank_weights``, when given, replaces
            the profile's weights.
        expand_for: the question as the person asked it. When given and it
            asks for completeness ("all", "list", "rows 8-10"), the chunks next
            to a table-like hit are added beside it (:mod:`axiom.rag.neighbours`),
            and the best chunks of the ``cited`` sources lead. The result may
            then hold more than ``limit`` chunks. Every added chunk passes the
            same access filter.
        cited: source paths the conversation already cited; with
            ``cited_suffixes``, path endings an earlier answer printed. Used only
            with ``expand_for``.
        keyword_only_corpora: corpora searched by keyword only, because the
            model they recorded is not the query's (``QueryEmbedding.keyword_only``).
            The vector ranking leaves them out; the other corpora keep it, and
            every ranking still fuses through RRF (P0.2).
        corpus_generation: read this generation of every corpus instead of
            the one each corpus serves (its active generation). For evaluating
            a candidate before ``promote``; serving callers leave it ``None``
            and the store reads the active generation (#1945).
    """
    # 0. Resolve the access filter BEFORE anything else, so there is one place
    # that decides and no later branch can reach the un-resolved argument.
    # Omission and None both land on the fail-closed baseline; only the named
    # UNFILTERED sentinel disables the filter.
    if isinstance(access_context, _Unfiltered):
        _ctx: AccessContext | None = None
    elif isinstance(access_context, (_Unset, type(None))):
        _ctx = AccessContext()
    else:
        _ctx = access_context

    # 0b. Per-item access (prd-sharing-and-access SHR-R8.3): a source with its own access rule
    # names the corpora that hold what was shared with this person, and decides each chunk it
    # owns. Only with an access context: UNFILTERED means a remote store already decided.
    from axiom.rag import source_guards

    guarded = _ctx is not None
    principal = getattr(_ctx, "principal", None) if guarded else None
    extra: set[str] = set()
    search_corpora = corpora
    if guarded and corpora is not None:
        extra = source_guards.extra_corpora(principal) - set(corpora)
        if extra:
            search_corpora = list(corpora) + sorted(extra)

    # The site's ranking declarations, read once: they decide the breadth and the extra
    # rankings below as well as the rerank.
    prof = profile if profile is not None else active_profile()
    breadth = prof.retrieval_breadth or retrieval_breadth

    # 1. Gather rankings. A generation is passed only when one is named, so a
    # store whose search() predates the keyword keeps working.
    gen_kw: dict[str, int] = {} if corpus_generation is None else {"corpus_generation": int(corpus_generation)}
    vector_hits: list[SearchResult] = []
    text_hits: list[SearchResult] = []
    vector_corpora = _vector_corpora(search_corpora, keyword_only_corpora)
    if query_embedding is not None and vector_corpora != []:
        vector_hits = store.search(
            query_embedding=query_embedding,
            query_text="",
            corpora=vector_corpora,
            limit=breadth,
            **gen_kw,
        )
    if query_text.strip():
        text_hits = store.search(
            query_embedding=None,
            query_text=query_text,
            corpora=search_corpora,
            limit=breadth,
            **gen_kw,
        )
    # 1b. Precise text rankings for the identifiers the question names (site profile).
    extra_rankings = _identifier_rankings(store, query_text, search_corpora, breadth, prof, gen_kw)
    # 1c. The sources the question names, by title or path (site profile).
    # A corpus searched by keyword only must not be ordered by a vector from another model either.
    name_hits = _name_hits(
        store, query_text, None if keyword_only_corpora else query_embedding, search_corpora, prof, gen_kw
    )

    # 2. Build identity→result map + list-of-rankings for RRF.
    by_key: dict[tuple[str, int, str], SearchResult] = {}
    for hit in [*vector_hits, *text_hits, *(h for r in extra_rankings for h in r), *name_hits]:
        by_key.setdefault(_chunk_key(hit), hit)

    rankings = [
        [_chunk_key(r) for r in vector_hits],
        [_chunk_key(r) for r in text_hits],
        *([_chunk_key(r) for r in ranking] for ranking in extra_rankings),
    ]
    fused = reciprocal_rank_fusion(rankings, k=60, limit=None)
    if prof.dedup_prefix_chars:
        fused = _dedup_by_prefix(fused, by_key, prof.dedup_prefix_chars)

    # 2b. Heuristic rerank. Folds hybrid.py's reranker onto this store-abstracted
    # path (previously it was RRF-only and hybrid.py's rerank ran nowhere): reorder
    # the fused candidates so value-bearing, on-topic chunks beat generic prose,
    # using the SAME signals as the hybrid stack. Access filter + citation keys
    # then run in reranked order.
    if rerank:
        prof = profile if profile is not None else active_profile()
        fused = _rerank_fused(
            query_text, fused, by_key, rerank_weights or prof.weights, prof
        )

    # 2b'. A named document's best chunks take reserved places at the front; they still pass
    # the access filter below like every other candidate.
    lead = _reserve_named(fused, name_hits, prof.name_reserved)
    fused = lead + fused

    # 2c. What the guards permit, decided once for every candidate they claim.
    allowed: set[str] = set()
    if guarded:
        claimed = {by_key[fr.doc_id].source_path for fr in fused  # type: ignore[index]
                   if source_guards.claimant(by_key[fr.doc_id].source_path)}  # type: ignore[index]
        allowed = source_guards.permitted(principal, claimed) if claimed else set()

    # 3. Access filter + citation-key assignment.
    def admit(hit: SearchResult, rrf_score: float) -> RetrievedChunk | None:
        """``hit`` as a chunk the caller may read, or ``None``. Keys are assigned at the end."""
        path = hit.source_path
        if guarded:
            owned = source_guards.claimant(path) is not None
            if owned and path not in allowed:
                return None  # its source says this person may not read it
            if not owned and hit.corpus in extra:
                return None  # an extra corpus is searched only for what a guard vouches for
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
        if _ctx is not None and not _permits(
            _ctx, tier, classification, nationalities, site
        ):
            return None
        return RetrievedChunk(
            citation_key="",
            rank=0,
            source_path=hit.source_path,
            source_title=hit.source_title,
            chunk_text=hit.chunk_text,
            chunk_index=hit.chunk_index,
            corpus=hit.corpus,
            similarity=hit.similarity,
            rrf_score=rrf_score,
            access_tier=tier,
            classification=classification,
            allowed_nationalities=nationalities,
            site=site,
        )

    out: list[RetrievedChunk] = []
    emitted: set = set()
    reserved = 0
    for pos, fr in enumerate(fused):
        if len(out) >= limit:
            break
        if fr.doc_id in emitted:
            continue
        if pos < len(lead) and reserved >= prof.name_reserved:
            continue  # the reserved places are taken; this chunk keeps its ordinary place
        chunk = admit(by_key[fr.doc_id], fr.score)  # type: ignore[index]
        if chunk is None:
            continue
        emitted.add(fr.doc_id)
        if pos < len(lead):
            reserved += 1
        out.append(chunk)

    # 4. Completeness (#1984 step 1c): the cited documents' best chunks lead, and a table-like
    # hit brings the chunks next to it. Both only when the caller passes the question.
    if expand_for:
        out = _expand(store, out, query_text, expand_for, cited, cited_suffixes, search_corpora, admit, gen_kw)

    return [replace(c, citation_key=f"C{i}", rank=i) for i, c in enumerate(out, start=1)]


@dataclass(frozen=True)
class Retrieval:
    """What :func:`retrieve_for` returns: the ranked chunks, and how the query was searched."""

    chunks: list[RetrievedChunk]
    #: ``True`` when a query vector took part (vector + text); ``False`` for text only.
    hybrid: bool
    #: What to tell the person about the search (the embedding guard's note), or ``""``.
    note: str = ""


def retrieve_for(
    store: _StoreLike,
    query_text: str,
    *,
    principal: str | None,
    routing_tier: str = "any",
    corpora: Sequence[str] | None = None,
    limit: int = 8,
    rerank: bool | None = None,
    expand_for: str | None = None,
) -> Retrieval:
    """Retrieve for a surface that has a question and a principal: the one managed path (#1984).

    Embeds the query through :func:`axiom.rag.embeddings.embed_query` at ``routing_tier`` and
    held to each corpus's recorded model (skipped for a store that runs hybrid search itself),
    then ranks through :func:`retrieve` with the principal's fail-closed access context. A
    surface that calls this ranks exactly as the chat does, so a ranking fix reaches it with
    no change of its own. ``rerank=None`` passes nothing, so the retriever's default governs.
    """
    vector, keyword_only, note = None, (), ""
    corpus_list = list(corpora) if corpora is not None else None
    if not getattr(store, "does_own_hybrid", False):
        from axiom.rag.embeddings import embed_query

        embedded = embed_query(query_text, routing_tier=routing_tier, store=store, corpora=corpus_list)
        vector, keyword_only, note = embedded.vector, embedded.keyword_only, embedded.note
    kwargs: dict = {} if rerank is None else {"rerank": rerank}
    chunks = retrieve(
        store,
        query_text,
        vector,
        corpora=corpus_list,
        limit=limit,
        access_context=access_context_for_principal(principal),
        expand_for=expand_for,
        keyword_only_corpora=keyword_only,
        **kwargs,
    )
    return Retrieval(chunks=chunks, hybrid=vector is not None, note=note)


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


def permits(
    ctx: AccessContext,
    *,
    access_tier: str,
    classification: str,
    allowed_nationalities: tuple[str, ...] | list[str] | None = None,
    site: str | None = None,
) -> bool:
    """Whether ``ctx`` may see one item with these access labels.

    The same rule retrieval applies to every chunk, exposed so that an extension keeping its
    own rows (outside the RAG store) can enforce it instead of re-implementing it. An unknown
    tier is denied, and a set ``ctx.site`` denies unattributed items, as in :func:`retrieve`.
    """
    nationalities = tuple(allowed_nationalities) if allowed_nationalities is not None else None
    return _permits(ctx, access_tier, classification, nationalities, site)


def visible_access_tiers(ctx: AccessContext) -> tuple[str, ...]:
    """The access tiers ``ctx`` may read, lowest first.

    For filtering in a query before ranking (``access_tier = ANY(...)``), so that rows the
    caller may not see never reach the ranking step. Classification, nationality and site
    still need :func:`permits` or the equivalent conditions.
    """
    ceiling = _TIER_ORDER.get(ctx.max_access_tier, 0)
    return tuple(t for t, order in sorted(_TIER_ORDER.items(), key=lambda kv: kv[1]) if order <= ceiling)


def _permits(
    ctx: AccessContext,
    tier: str,
    classification: str,
    nationalities: tuple[str, ...] | None,
    site: str | None = None,
) -> bool:
    if _TIER_ORDER.get(tier, 99) > _TIER_ORDER.get(ctx.max_access_tier, 0):
        return False
    from axiom.governance.classification import Classification, classification_lte, parse_label

    label = parse_label(classification)
    if label is not None:
        # An export-control label: compared with the person's clearance, the
        # floor when none was resolved (MVP-06-A1).
        clearance = parse_label(ctx.clearance) or Classification.PUBLIC
        if not classification_lte(label, clearance):
            return False
    elif classification not in ctx.allowed_classifications:
        return False
    if nationalities is not None and ctx.nationality not in nationalities:
        return False
    if ctx.site is not None and site != ctx.site and site != SHARED_SITE:
        # Fail closed, including when the chunk's tenant is unknown: an
        # unattributed chunk belongs to nobody, which is not the same as
        # belonging to whoever happens to be asking.
        return False
    return True
