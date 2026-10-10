# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Domain-agnostic RAG corpus health helper.

Collects per-corpus health metrics so operator-facing surfaces (notably
``axi config --status``) can show *more* than "credential configured /
missing" — e.g. is the corpus actually populated, when did we last ingest
it, what embedding model was used, and how is recent retrieval scoring.

The store it reads is the configured Postgres retrieval store (ADR-174), through a read-only
connection: a health check has no business writing to, or migrating, what it inspects.

Tolerance is the headline guarantee: if no store is configured, or it is unreachable, empty,
or not a Postgres store, ``collect_rag_health`` MUST return a well-formed ``RagHealth`` rather
than raise.  Operator surfaces should always be able to call this without a ``try/except``
wrapper.

Per axiom-domain-agnostic rule: rendered output names corpus IDs only —
no domain-consumer terminology (no domain/site/host-name leakage).  Per
``feedback_rich_console_lazy_construction``: any ``rich.Console`` is
constructed inside ``render_rag_health`` (not at module import) so
capsys can capture the output in tests.
"""

from __future__ import annotations

import json
import logging
import os
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


def _brand_cli() -> str:
    """The command the operator actually typed.

    These lines said "axi" unconditionally, so a consumer distribution's CLI
    told the operator to run a command that does not exist on their machine.
    """
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CorpusHealth:
    """Per-corpus health snapshot.

    Fields surface as ``None`` when the underlying datum cannot be derived
    from the store — operator surfaces should treat ``None`` as "unknown"
    rather than "zero".
    """

    corpus_id: str
    chunk_count: int
    last_ingested_at: str | None  # ISO-8601 with offset, or None
    embedding_model: str | None
    active_generation: str | None
    recent_retrieval_p50_score: float | None  # last-N median top-score
    recent_retrieval_count: int  # last 24h count


@dataclass(frozen=True)
class RagHealth:
    """Aggregate RAG-store health snapshot."""

    corpora: tuple[CorpusHealth, ...]
    total_chunks: int
    healthy: bool  # at least one corpus with chunk_count > 0


_EMPTY = RagHealth(corpora=(), total_chunks=0, healthy=False)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def default_database_url() -> str:
    """The configured retrieval store URL: ``DATABASE_URL`` first, then the ``rag.database_url``
    setting (the same order ``axi rag`` and the MCP retrieval primitive use)."""
    url = os.environ.get("DATABASE_URL") or ""
    if url:
        return url
    try:
        from axiom.extensions.builtins.settings.store import SettingsStore

        return SettingsStore().get("rag.database_url", "") or ""
    except Exception:  # noqa: BLE001
        return ""


def collect_rag_health(
    database_url: str | None = None,
    *,
    known_corpora: Sequence[str] | None = None,
    embedding_model_hint: str | None = None,
) -> RagHealth:
    """Read the Postgres retrieval store and return aggregate health.

    Parameters
    ----------
    database_url:
        A ``postgresql://`` store URL.  ``None`` means the configured store
        (:func:`default_database_url`).  No store, an unreachable one, or a
        non-Postgres URL yields an empty ``RagHealth``.
    known_corpora:
        Optional list of corpus IDs the caller wants surfaced even when
        empty.  Useful for operator displays that want to distinguish
        "configured but empty" from "doesn't exist."
    embedding_model_hint:
        The active embedding model name.  We don't persist this in the
        store schema today, so callers (e.g. the wizard) can pass the
        value they would have used at ingest time.

    Returns
    -------
    RagHealth
        Always a well-formed value.  Never raises.
    """
    url = database_url if database_url is not None else default_database_url()
    if not url.startswith(("postgresql://", "postgres://")):
        return _maybe_known_corpora(_EMPTY, known_corpora, embedding_model_hint)

    try:
        import psycopg2

        conn = psycopg2.connect(url, connect_timeout=5)
        conn.set_session(readonly=True, autocommit=True)
    except Exception as exc:  # noqa: BLE001
        log.debug("collect_rag_health: cannot connect: %s", exc)
        return _maybe_known_corpora(_EMPTY, known_corpora, embedding_model_hint)

    try:
        if not _has_rag_tables(conn):
            return _maybe_known_corpora(_EMPTY, known_corpora, embedding_model_hint)
        per_corpus = _read_chunk_summaries(conn)
        audit_by_corpus = _read_recent_retrieval_scores(conn)
    except Exception as exc:  # noqa: BLE001
        log.debug("collect_rag_health: read failed: %s", exc)
        return _maybe_known_corpora(_EMPTY, known_corpora, embedding_model_hint)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001  # pragma: no cover — defensive
            pass

    corpus_ids = set(per_corpus.keys())
    if known_corpora:
        corpus_ids.update(known_corpora)

    corpora: list[CorpusHealth] = []
    for cid in sorted(corpus_ids):
        summary = per_corpus.get(cid)
        chunk_count = summary["chunk_count"] if summary else 0
        last = summary["last_indexed"] if summary else None
        gen = summary["active_generation"] if summary else None
        score, count = audit_by_corpus.get(cid, (None, 0))

        # We use the global retrieval-audit window when the audit log
        # doesn't tag rows by corpus (current schema).  Surface the same
        # value on every corpus so operators can read "the system has
        # served N retrievals at p50 X" alongside per-corpus inventory.
        if cid not in audit_by_corpus and audit_by_corpus.get("__global__"):
            score, count = audit_by_corpus["__global__"]

        corpora.append(
            CorpusHealth(
                corpus_id=cid,
                chunk_count=chunk_count,
                last_ingested_at=last,
                embedding_model=embedding_model_hint,
                active_generation=gen,
                recent_retrieval_p50_score=score,
                recent_retrieval_count=count,
            )
        )

    total = sum(c.chunk_count for c in corpora)
    healthy = any(c.chunk_count > 0 for c in corpora)
    return RagHealth(corpora=tuple(corpora), total_chunks=total, healthy=healthy)


def render_rag_health(health: RagHealth) -> None:
    """Print a human-friendly RAG section to stdout.

    Constructs ``rich.Console`` lazily so capsys captures the output in
    tests.  Falls back to plain ``print`` if rich isn't importable.
    """
    try:  # pragma: no cover — import guard
        from rich.console import Console

        console = Console(force_terminal=False, highlight=False)
        printer = console.print
    except Exception:  # pragma: no cover — fall back to stdlib
        def printer(msg: str = "") -> None:
            print(msg)

    printer("")
    printer("  RAG")
    printer("  ---")

    if not health.corpora:
        printer(f"    no corpora detected — run `{_brand_cli()} rag ingest` to populate")
        return

    for c in health.corpora:
        printer(f"    corpus: {c.corpus_id}")
        printer(f"      chunks: {c.chunk_count:,}")
        if c.chunk_count == 0:
            printer(f"      (empty — run `{_brand_cli()} rag ingest` to populate)")
            continue
        if c.last_ingested_at:
            printer(f"      last ingested: {c.last_ingested_at}")
        if c.embedding_model:
            printer(f"      embedding: {c.embedding_model}")
        if c.active_generation:
            printer(f"      active generation: {c.active_generation}")
        if c.recent_retrieval_count > 0 and c.recent_retrieval_p50_score is not None:
            printer(
                f"      p50 retrieval score (24h): "
                f"{c.recent_retrieval_p50_score:.2f}  "
                f"({c.recent_retrieval_count} queries)"
            )
        else:
            printer("      p50 retrieval score (24h): n/a (no recent queries)")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _has_rag_tables(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name IN ('chunks', 'documents')"
        )
        names = {row[0] for row in cur.fetchall()}
    # Either chunks alone or documents alone is enough to surface partial
    # health; we don't require both.
    return bool(names)


def _read_chunk_summaries(conn) -> dict[str, dict]:
    """Return per-corpus chunk_count + last_indexed + active_generation."""
    out: dict[str, dict] = {}

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT corpus, COUNT(*), MAX(corpus_generation) FROM chunks GROUP BY corpus"
            )
            for corpus, n, gen in cur.fetchall():
                out[corpus] = {
                    "chunk_count": int(n or 0),
                    "active_generation": str(gen) if gen is not None else None,
                    "last_indexed": None,
                }
    except Exception as exc:  # noqa: BLE001
        log.debug("chunk summary failed: %s", exc)

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT corpus, MAX(last_indexed) FROM documents GROUP BY corpus")
            for corpus, last in cur.fetchall():
                entry = out.setdefault(
                    corpus,
                    {"chunk_count": 0, "active_generation": None, "last_indexed": None},
                )
                entry["last_indexed"] = last.isoformat() if last is not None else None
    except Exception as exc:  # noqa: BLE001
        log.debug("document summary failed: %s", exc)

    return out


def _read_recent_retrieval_scores(conn) -> dict[str, tuple[float | None, int]]:
    """Return median top-score + count of retrieval-audit rows in last 24h.

    The current ``retrieval_audit`` schema does not tag rows by corpus, so
    we surface a single ``__global__`` bucket; ``collect_rag_health``
    fans the value out to every corpus.
    """
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT retrieved_chunks FROM retrieval_audit WHERE created_at >= %s", (cutoff,)
            )
            rows = [r[0] for r in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        log.debug("retrieval_audit read failed: %s", exc)
        return {}

    top_scores = list(_iter_top_scores(rows))
    if not top_scores:
        return {"__global__": (None, 0)}
    p50 = float(statistics.median(top_scores))
    return {"__global__": (p50, len(top_scores))}


def _iter_top_scores(rows: Iterable) -> Iterable[float]:
    """Best score per audit row. Each item is the stored ``retrieved_chunks`` value: JSON text,
    or already decoded when the column is JSONB."""
    for raw in rows:
        try:
            payload = json.loads(raw or "[]") if isinstance(raw, str | bytes | None) else raw
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, list) or not payload:
            continue
        # Top score = best rrf_score (or similarity if no rrf).
        best: float | None = None
        for chunk in payload:
            if not isinstance(chunk, dict):
                continue
            score = chunk.get("rrf_score")
            if score is None:
                score = chunk.get("similarity")
            if score is None:
                continue
            try:
                f = float(score)
            except (TypeError, ValueError):
                continue
            if best is None or f > best:
                best = f
        if best is not None:
            yield best


def _maybe_known_corpora(
    base: RagHealth,
    known: Sequence[str] | None,
    embedding_hint: str | None,
) -> RagHealth:
    """Return ``base`` augmented with empty entries for ``known`` corpora."""
    if not known:
        return base
    extras = tuple(
        CorpusHealth(
            corpus_id=cid,
            chunk_count=0,
            last_ingested_at=None,
            embedding_model=embedding_hint,
            active_generation=None,
            recent_retrieval_p50_score=None,
            recent_retrieval_count=0,
        )
        for cid in sorted(set(known))
    )
    return RagHealth(
        corpora=extras,
        total_chunks=base.total_chunks,
        healthy=base.healthy,
    )
