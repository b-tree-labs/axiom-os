# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Shared hybrid retrieval: dense + BM25 + entity recall -> RRF -> rerank.

The ONE retrieval module a site's serving layer imports for both its chat path
and its ``/api/v1/rag/search`` endpoint:

* spec-harness-agnostic-site-access, P1 "One retrieval, served and logged":
  extract a single shared retrieval module (both serving shims import it) and
  emit ``retrieval_log`` events from the first request, so the site owner's
  harness "retrieves byte-identical chunk sets to the hosted chat, and both
  appear in one log".
* spec-unified-mcp-surface, amendment #1: "one shared retrieval module ...
  not a third implementation. 'Same RAG as the chat' must hold by
  construction."

Chat and search call :func:`retrieve` with the same :class:`HybridConfig`, so
they return identical chunk sets, and both append to one ``retrieval_log``
through :func:`make_retrieval_log_sink`.

Driver-agnostic by design. ``retrieve`` takes any DB-API 2.0 connection with
the ``%s`` paramstyle (psycopg2, which :mod:`axiom.rag.store` uses, or
psycopg 3, which a lightweight serving venv carries) and imports no driver, no
SQLAlchemy, and nothing from identity or federation. It stays importable where
only a psycopg is installed. It reuses :mod:`axiom.rag.rrf` (fusion) and
:mod:`axiom.rag.fts_query` (tsquery construction, shared with ``RAGStore``);
the candidate SQL is carried here because ``RAGStore.search`` is a psycopg2
class that fuses inside one statement with linear weights rather than
returning the separate rankings RRF needs.

Nothing here names a site. Site specifics arrive as :class:`HybridConfig`
fields: corpora, access tiers, a dense-similarity floor, an authority corpus
and/or path pattern, extra code tokens, and the value-intent vocabulary.

Pipeline, per call:

1. dense candidates   -- pgvector cosine over ``chunks`` (skipped without an
                         embedding or in ``mode="text"``); optional
                         ``min_score`` floor on the similarity;
2. text candidates    -- scan-bounded ``ts_rank`` over an OR-of-terms tsquery
                         (skipped in ``mode="dense"``);
3. entity candidates  -- the same ranking over an entity-required tsquery when
                         the query names an identifier;
4. RRF fusion         -- :func:`axiom.rag.rrf.reciprocal_rank_fusion`;
5. rerank             -- query-term coverage, value intent, authority (corpus
                         or path/title match), normalised RRF;
6. dedup by text prefix, cap at ``k``.

Each text ranking is fail-soft: a statement timeout or tsquery error rolls
back, drops that ranking (noted on the log record), and the others carry the
turn. The caller owns the connection and its transaction; ``retrieve`` only
reads, and ``SET statement_timeout`` / ``SET ivfflat.probes`` are applied on
the caller's session before the first candidate query.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from functools import cached_property
from typing import Any, Literal

from axiom.rag.fts_query import fts_entity_tsquery, fts_terms, fts_tsquery
from axiom.rag.rrf import reciprocal_rank_fusion

log = logging.getLogger(__name__)

Mode = Literal["hybrid", "dense", "text"]
_MODES: tuple[str, ...] = ("hybrid", "dense", "text")

#: Chunk store table (an Axiom ``RAGStore`` schema: see ``axiom.rag.store``).
CHUNKS_TABLE = "chunks"
#: Default audit-log table written by :func:`make_retrieval_log_sink`.
RETRIEVAL_LOG_TABLE = "retrieval_log"
#: Two chunks whose text shares this prefix are the same passage (near-dup
#: chunking of one source); only the better-ranked one is returned.
DEDUP_PREFIX_CHARS = 120
#: Query words that signal the user wants a quantity; a candidate that actually
#: carries one (currency sign, "cents", a two-decimal number) is then boosted.
DEFAULT_VALUE_INTENT_TERMS: tuple[str, ...] = (
    "worth",
    "value",
    "values",
    "dollar",
    "dollars",
    "cents",
    "limit",
    "exceed",
    "cost",
    "much",
    "magnitude",
    "measured",
)
_VALUE_TOKEN_RE = re.compile(r"\$|\bcents\b|-?\d+\.\d{2}")
_SQL_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$")

# Backend row shape, both rankings:
#   (source_path, source_title, chunk_text, chunk_index, corpus, score)
_Row = tuple[Any, ...]
_Key = tuple[str, int, str]


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RerankWeights:
    """Rerank signal weights. Defaults are the measured site values."""

    coverage: float = 3.0
    value_intent: float = 1.5
    corpus_authority: float = 1.0
    rrf: float = 1.0


@dataclass(frozen=True)
class HybridConfig:
    """Everything that varies per site, per surface, or per call.

    ``access_tiers`` is the fail-closed gate on an unauthenticated surface:
    only chunks whose ``access_tier`` is listed are candidates. An empty tuple
    disables the filter (for a caller that applies its own access policy).
    ``min_score`` floors the dense similarity before fusion and never touches
    text-only candidates. ``authority_corpus`` / ``authority_path_re`` mark a
    chunk as authoritative for the rerank (corpus match, or the pattern found
    in ``source_path`` or ``source_title``). ``extra_code_re`` adds code tokens
    the default lexer cannot see (hyphenated or all-letter abbreviations).
    ``statement_timeout_ms=0`` disables the timeout.
    """

    corpora: tuple[str, ...]
    k: int = 8
    breadth: int = 40
    fusion_k: int = 60
    min_score: float | None = None
    authority_corpus: str | None = None
    authority_path_re: str | None = None
    rerank_weights: RerankWeights = field(default_factory=RerankWeights)
    access_tiers: tuple[str, ...] = ("public",)
    statement_timeout_ms: int = 2500
    ivfflat_probes: int | None = None
    mode: Mode = "hybrid"
    rerank: bool = True
    text_scan_cap: int = 4000
    extra_code_re: str | None = None
    value_intent_terms: tuple[str, ...] = DEFAULT_VALUE_INTENT_TERMS

    def __post_init__(self) -> None:
        if not self.corpora:
            raise ValueError("corpora must name at least one corpus")
        if self.k <= 0:
            raise ValueError(f"k must be positive; got {self.k}")
        if self.breadth < self.k:
            raise ValueError(f"breadth ({self.breadth}) must be >= k ({self.k})")
        if self.fusion_k <= 0:
            raise ValueError(f"fusion_k must be positive; got {self.fusion_k}")
        if self.mode not in _MODES:
            raise ValueError(f"mode must be one of {_MODES}; got {self.mode!r}")
        if self.statement_timeout_ms < 0:
            raise ValueError("statement_timeout_ms must be >= 0 (0 disables the timeout)")
        if self.ivfflat_probes is not None and self.ivfflat_probes <= 0:
            raise ValueError(f"ivfflat_probes must be positive; got {self.ivfflat_probes}")
        if self.text_scan_cap <= 0:
            raise ValueError(f"text_scan_cap must be positive; got {self.text_scan_cap}")
        _compile(self.authority_path_re, "authority_path_re")
        _compile(self.extra_code_re, "extra_code_re")

    @cached_property
    def authority_pattern(self) -> re.Pattern[str] | None:
        """``authority_path_re`` compiled once per config."""
        return _compile(self.authority_path_re, "authority_path_re")

    @cached_property
    def extra_code_pattern(self) -> re.Pattern[str] | None:
        """``extra_code_re`` compiled once per config."""
        return _compile(self.extra_code_re, "extra_code_re")


def _compile(pattern: str | None, name: str) -> re.Pattern[str] | None:
    if pattern is None:
        return None
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"{name} is not a valid regular expression: {exc}") from exc


@dataclass(frozen=True)
class Hit:
    """One returned chunk. ``similarity`` is the dense cosine similarity (0.0
    for a chunk that only the text rankings recalled); ``combined_score`` is
    the rerank score, or the RRF score when rerank did not run."""

    source_path: str
    source_title: str
    chunk_text: str
    chunk_index: int
    corpus: str
    similarity: float
    combined_score: float
    rank: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": self.source_path,
            "source_title": self.source_title,
            "chunk_text": self.chunk_text,
            "chunk_index": self.chunk_index,
            "corpus": self.corpus,
            "similarity": self.similarity,
            "combined_score": self.combined_score,
            "rank": self.rank,
        }

    def to_log_entry(self) -> dict[str, Any]:
        """Coarse identity + score for the retrieval log. No chunk body, so the
        log carries no tier-sensitive content and is safe for analyst reads."""
        return {
            "title": self.source_title,
            "path": self.source_path,
            "chunk_index": self.chunk_index,
            "corpus": self.corpus,
            "score": round(float(self.combined_score), 6),
        }


@dataclass(frozen=True)
class RetrievalRecord:
    """One ``retrieval_log`` row. ``results`` holds :meth:`Hit.to_log_entry`
    dicts in returned order. ``principal`` / ``source`` are stamped by the
    caller (see :func:`bind_log_sink`)."""

    query_text: str
    mode: str
    k: int
    result_count: int
    latency_ms: float
    results: tuple[dict[str, Any], ...]
    note: str | None = None
    principal: str | None = None
    source: str = "rag_search"


LogSink = Callable[[RetrievalRecord], None]


# ---------------------------------------------------------------------------
# Candidate SQL
# ---------------------------------------------------------------------------


def _tier_clause(cfg: HybridConfig) -> str:
    return " AND access_tier = ANY(%s)" if cfg.access_tiers else ""


def _tier_params(cfg: HybridConfig) -> list[list[str]]:
    return [list(cfg.access_tiers)] if cfg.access_tiers else []


def _dense_sql(cfg: HybridConfig) -> str:
    return (
        "SELECT source_path, source_title, chunk_text, chunk_index, corpus,\n"
        "       1 - (embedding <=> %s::vector) AS similarity\n"
        f"FROM {CHUNKS_TABLE}\n"
        f"WHERE corpus = ANY(%s) AND embedding IS NOT NULL{_tier_clause(cfg)}\n"
        "ORDER BY embedding <=> %s::vector\n"
        "LIMIT %s"
    )


# Scan-bounded: the inner query stops the GIN scan at text_scan_cap hits (no
# ORDER, so ts_rank is computed only for those), then the outer ranks that set.
# ORDER BY ts_rank therefore never ranks every match of a broad OR query.
def _text_sql(cfg: HybridConfig) -> str:
    return (
        "SELECT source_path, source_title, chunk_text, chunk_index, corpus, text_rank\n"
        "FROM (\n"
        "    SELECT source_path, source_title, chunk_text, chunk_index, corpus,\n"
        "           ts_rank(to_tsvector('english', chunk_text),\n"
        "                   to_tsquery('english', %s)) AS text_rank\n"
        f"    FROM {CHUNKS_TABLE}\n"
        f"    WHERE corpus = ANY(%s){_tier_clause(cfg)}\n"
        "      AND to_tsvector('english', chunk_text) @@ to_tsquery('english', %s)\n"
        "    LIMIT %s\n"
        ") hits\n"
        "ORDER BY text_rank DESC\n"
        "LIMIT %s"
    )


def _vector_literal(embedding: Sequence[float] | None) -> str | None:
    if not embedding:
        return None
    return "[" + ",".join(f"{float(x):.6f}" for x in embedding) + "]"


def _apply_session_settings(cur: Any, cfg: HybridConfig) -> None:
    # Hard per-call ceiling so a chat turn never hangs on retrieval; 0 disables.
    cur.execute(f"SET statement_timeout = {int(cfg.statement_timeout_ms)}")
    if cfg.ivfflat_probes is not None:
        cur.execute(f"SET ivfflat.probes = {int(cfg.ivfflat_probes)}")


def _run_text_ranking(
    conn: Any, cur: Any, cfg: HybridConfig, tsquery: str, label: str, notes: list[str]
) -> list[_Row]:
    """One bounded text ranking. On timeout or a tsquery error, roll back the
    aborted statement, re-apply the session settings the rollback reverted, and
    drop this ranking so dense and the other ranking carry the turn."""
    params = (
        tsquery,
        list(cfg.corpora),
        *_tier_params(cfg),
        tsquery,
        cfg.text_scan_cap,
        cfg.breadth,
    )
    try:
        cur.execute(_text_sql(cfg), params)
        return list(cur.fetchall())
    except Exception as exc:  # noqa: BLE001 -- fail-soft by contract
        notes.append(f"{label} ranking dropped: {type(exc).__name__}: {exc}")
        log.warning("hybrid retrieval: %s ranking dropped (%s: %s)", label, type(exc).__name__, exc)
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        _apply_session_settings(cur, cfg)
        return []


# ---------------------------------------------------------------------------
# Fusion + rerank (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    row: _Row
    rrf: float
    similarity: float


def _key(row: _Row) -> _Key:
    """Chunk identity under fusion: (source_path, chunk_index, corpus)."""
    return (row[0], row[3], row[4])


def _fuse(
    dense_rows: list[_Row], text_rows: list[_Row], entity_rows: list[_Row], cfg: HybridConfig
) -> list[_Candidate]:
    """RRF-fuse the rankings into one best-first candidate list, deduplicated
    by text prefix. Dense similarity is kept per chunk for the Hit."""
    by_key: dict[_Key, _Row] = {}
    similarity: dict[_Key, float] = {}
    for row in dense_rows:
        k = _key(row)
        by_key.setdefault(k, row)
        similarity.setdefault(k, float(row[5] or 0.0))
    for rows in (text_rows, entity_rows):
        for row in rows:
            by_key.setdefault(_key(row), row)

    rankings = [[_key(r) for r in rows] for rows in (dense_rows, text_rows, entity_rows)]
    fused = reciprocal_rank_fusion(rankings, k=cfg.fusion_k)

    seen: set[str] = set()
    out: list[_Candidate] = []
    for fr in fused:
        row = by_key[fr.doc_id]  # type: ignore[index]
        prefix = (row[2] or "")[:DEDUP_PREFIX_CHARS]
        if prefix in seen:
            continue
        seen.add(prefix)
        out.append(_Candidate(row=row, rrf=fr.score, similarity=similarity.get(fr.doc_id, 0.0)))  # type: ignore[arg-type]
    return out


def _rerank(
    query: str, cands: list[_Candidate], cfg: HybridConfig
) -> list[tuple[_Candidate, float]]:
    """Reorder fused candidates so value-bearing, on-topic, authoritative
    chunks beat generic prose. Falls back to RRF order (score = RRF) when
    rerank is off or the query carries no content terms."""
    qterms = set(fts_terms(query, extra_code_re=cfg.extra_code_pattern))
    if not cfg.rerank or not qterms:
        return [(c, c.rrf) for c in cands[: cfg.k]]

    w = cfg.rerank_weights
    pattern = cfg.authority_pattern
    value_q = bool(qterms & set(cfg.value_intent_terms))
    max_rrf = max((c.rrf for c in cands), default=0.0)

    def score(c: _Candidate) -> float:
        path, title, text, _idx, corpus, _s = c.row
        text = text or ""
        coverage = len(qterms & set(fts_terms(text, extra_code_re=cfg.extra_code_pattern))) / len(
            qterms
        )
        value = 1.0 if (value_q and _VALUE_TOKEN_RE.search(text)) else 0.0
        authoritative = (cfg.authority_corpus is not None and corpus == cfg.authority_corpus) or (
            pattern is not None
            and (bool(pattern.search(path or "")) or bool(pattern.search(title or "")))
        )
        rrf_norm = c.rrf / max_rrf if max_rrf else 0.0
        return (
            w.coverage * coverage
            + w.value_intent * value
            + w.corpus_authority * (1.0 if authoritative else 0.0)
            + w.rrf * rrf_norm
        )

    scored = [(c, score(c)) for c in cands]
    scored.sort(key=lambda cs: cs[1], reverse=True)  # stable: ties keep fused order
    return scored[: cfg.k]


def _to_hits(ranked: list[tuple[_Candidate, float]]) -> list[Hit]:
    hits: list[Hit] = []
    for pos, (c, score) in enumerate(ranked, start=1):
        path, title, text, idx, corpus, _s = c.row
        hits.append(
            Hit(
                source_path=path or "",
                source_title=title or "",
                chunk_text=text or "",
                chunk_index=int(idx or 0),
                corpus=corpus or "",
                similarity=c.similarity,
                combined_score=float(score),
                rank=pos,
            )
        )
    return hits


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def retrieve(
    conn: Any,
    query: str,
    query_embedding: Sequence[float] | None,
    cfg: HybridConfig,
    *,
    log_sink: LogSink | None = None,
) -> list[Hit]:
    """Hybrid retrieval over ``chunks`` on a DB-API connection.

    ``query`` is the standalone (already conversation-rewritten) text used for
    the text rankings and the rerank; ``query_embedding`` is its dense vector
    (``None`` skips the dense ranking). ``log_sink`` receives exactly one
    :class:`RetrievalRecord` per call, after the hits are known; a sink that
    raises is logged and ignored, so telemetry never fails a turn.
    """
    t0 = time.perf_counter()
    query = query or ""
    notes: list[str] = []

    vec = _vector_literal(query_embedding)
    run_dense = cfg.mode in ("hybrid", "dense") and vec is not None
    if cfg.mode == "dense" and vec is None:
        notes.append("dense mode without an embedding: nothing to run")
    elif cfg.mode == "hybrid" and vec is None:
        notes.append("no embedding: dense ranking skipped")

    tsq = fts_tsquery(query, extra_code_re=cfg.extra_code_pattern) if cfg.mode != "dense" else ""
    entq = fts_entity_tsquery(query, extra_code_re=cfg.extra_code_pattern) if tsq else ""
    if entq == tsq:
        entq = ""

    dense_rows: list[_Row] = []
    text_rows: list[_Row] = []
    entity_rows: list[_Row] = []
    if run_dense or tsq:
        with conn.cursor() as cur:
            _apply_session_settings(cur, cfg)
            if run_dense:
                params = (vec, list(cfg.corpora), *_tier_params(cfg), vec, cfg.breadth)
                cur.execute(_dense_sql(cfg), params)
                dense_rows = list(cur.fetchall())
                if cfg.min_score is not None:
                    dense_rows = [
                        r for r in dense_rows if r[5] is not None and float(r[5]) >= cfg.min_score
                    ]
            if tsq:
                text_rows = _run_text_ranking(conn, cur, cfg, tsq, "text", notes)
            if entq:
                entity_rows = _run_text_ranking(conn, cur, cfg, entq, "entity", notes)

    hits = _to_hits(_rerank(query, _fuse(dense_rows, text_rows, entity_rows, cfg), cfg))
    latency_ms = (time.perf_counter() - t0) * 1000.0

    if log_sink is not None:
        record = RetrievalRecord(
            query_text=query,
            mode=cfg.mode,
            k=cfg.k,
            result_count=len(hits),
            latency_ms=round(latency_ms, 3),
            results=tuple(h.to_log_entry() for h in hits),
            note="; ".join(notes) or None,
        )
        try:
            log_sink(record)
        except Exception as exc:  # noqa: BLE001 -- telemetry never fails a turn
            log.warning("hybrid retrieval: log sink failed (%s: %s)", type(exc).__name__, exc)
    return hits


# ---------------------------------------------------------------------------
# retrieval_log: the trace-plane signal both surfaces write to
# ---------------------------------------------------------------------------


def bind_log_sink(sink: LogSink, *, principal: str | None, source: str = "rag_search") -> LogSink:
    """Wrap ``sink`` so every record it receives carries this caller's
    ``principal`` (coarse, never PII) and ``source`` (``rag_search``, ``chat``,
    ...)."""

    def bound(record: RetrievalRecord) -> None:
        sink(replace(record, principal=principal, source=source))

    return bound


def _check_table(table: str) -> str:
    if not _SQL_IDENT_RE.match(table or ""):
        raise ValueError(f"table must be a plain or schema-qualified identifier; got {table!r}")
    return table


def retrieval_log_ddl(table: str = RETRIEVAL_LOG_TABLE) -> str:
    """Idempotent DDL for the audit log. Index names derive from the bare
    table name so a schema-qualified ``table`` matches indexes an earlier
    migration already created. Grants are the node's business, not the
    platform's."""
    _check_table(table)
    bare = table.rsplit(".", 1)[-1]
    return (
        f"CREATE TABLE IF NOT EXISTS {table} (\n"
        "    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,\n"
        "    ts            timestamptz NOT NULL DEFAULT now(),\n"
        "    -- Coarse caller identity (a node id, a role label); never PII.\n"
        "    principal     text,\n"
        "    source        text NOT NULL DEFAULT 'rag_search',   -- rag_search | chat | ...\n"
        "    query_text    text NOT NULL,\n"
        "    mode          text NOT NULL DEFAULT 'hybrid',        -- hybrid | dense | text\n"
        "    k             int  NOT NULL,\n"
        "    result_count  int  NOT NULL,\n"
        "    -- Ordered coarse identities + scores of what was returned; no chunk bodies.\n"
        "    results       jsonb NOT NULL DEFAULT '[]'::jsonb,\n"
        "    latency_ms    double precision,\n"
        "    -- Non-fatal note when retrieval degraded (e.g. a text ranking timed out).\n"
        "    note          text\n"
        ");\n"
        f"CREATE INDEX IF NOT EXISTS idx_{bare}_ts ON {table} (ts DESC);\n"
        f"CREATE INDEX IF NOT EXISTS idx_{bare}_zero_hits ON {table} (ts DESC)"
        " WHERE result_count = 0;\n"
    )


#: DDL for the default table name; see :func:`retrieval_log_ddl`.
RETRIEVAL_LOG_DDL = retrieval_log_ddl()


def ensure_retrieval_log(conn: Any, *, table: str = RETRIEVAL_LOG_TABLE) -> None:
    """Create the audit log (and its indexes) if missing, then commit. Raises
    on failure: a node bootstrapping its trace plane wants to know."""
    ddl = retrieval_log_ddl(table)
    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()


def make_retrieval_log_sink(conn: Any, *, table: str = RETRIEVAL_LOG_TABLE) -> LogSink:
    """A :class:`RetrievalRecord` sink that appends one row to ``table`` on
    ``conn`` and commits. Best-effort by contract: any failure is logged as a
    warning (the flywheel is blind, ops should notice) and swallowed, and the
    aborted transaction is rolled back so the connection stays usable."""
    sql = (
        f"INSERT INTO {_check_table(table)} "
        "(principal, source, query_text, mode, k, result_count, results, latency_ms, note) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)"
    )

    def sink(record: RetrievalRecord) -> None:
        params = (
            record.principal,
            record.source,
            record.query_text,
            record.mode,
            record.k,
            record.result_count,
            json.dumps(list(record.results)),
            record.latency_ms,
            record.note,
        )
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()
        except Exception as exc:  # noqa: BLE001 -- telemetry never fails a turn
            log.warning(
                "%s write failed (flywheel signal lost): %s: %s", table, type(exc).__name__, exc
            )
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass

    return sink


__all__ = [
    "CHUNKS_TABLE",
    "DEDUP_PREFIX_CHARS",
    "DEFAULT_VALUE_INTENT_TERMS",
    "RETRIEVAL_LOG_DDL",
    "RETRIEVAL_LOG_TABLE",
    "Hit",
    "HybridConfig",
    "LogSink",
    "Mode",
    "RerankWeights",
    "RetrievalRecord",
    "bind_log_sink",
    "ensure_retrieval_log",
    "make_retrieval_log_sink",
    "retrieval_log_ddl",
    "retrieve",
]
