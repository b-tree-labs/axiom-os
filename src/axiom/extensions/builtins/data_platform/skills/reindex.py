# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""``data.reindex`` — offline re-index of already-landed bronze records.

The recovery + maintenance path: when content has already landed in bronze
(via a connector pull) but indexing didn't finish or needs to be rebuilt,
this re-runs extract/chunk/write over the bronze records of one source
without re-fetching from the source. Wraps :func:`reindex_bronze` (the tested orchestration
core); the embed unit is the same ``embed_bronze_record`` the live pipeline
uses, so reindex and first-pass ingest share one code path.

CLI: ``axi data reindex --source NAME --corpus NAME [--bronze-root PATH]``.

``axi data reindex --awaiting --corpus NAME`` instead embeds the corpus's chunks
stored without a vector (text-only because no embedder was admitted for their
label), from the stored text, once an embedder cleared for them exists (P0.2).

A record is written to the tier its bronze record names (``corpus`` only when
it names none, so an access tier is never crossed). It is skipped when its
``source_path`` is already a document in that corpus AND the text extractors the registry would run on it now are the
ones recorded on that document (``documents.parser``, name and version). A new
extractor version therefore reprocesses only the records it applies to, and a
second run with the same version skips them (#1947). A binary document indexed
before extractor records existed has none, so the first pass reprocesses it
once when the registry has an extractor for its format.

Interim shortcut. This reads bronze straight into the RAG and skips conform,
silver and gold. The designed route is bronze -> conform -> silver -> gold ->
RAG (ADR-079): the RAG is a projection of gold. Don't build new parsing,
access labelling or curation on this path; parsing belongs in conform and
labels and deduplication in gold curation.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from axiom.governance.classification import Classification
from axiom.infra.skills import SkillContext, SkillResult

from .. import _authz
from ..orchestration.reindex_bronze import reindex_bronze

log = logging.getLogger(__name__)


def bronze_root_for(params: dict[str, Any]) -> Path:
    """``bronze_root`` param, else ``DP1_BRONZE_ROOT``, else the data verbs' shared default."""
    from ..bronze import default_bronze_root

    explicit = params.get("bronze_root") or os.environ.get("DP1_BRONZE_ROOT")
    return Path(explicit).expanduser() if explicit else default_bronze_root()


def _iter_manifests(bronze_root: Path, source_name: str):
    """Yield bronze manifest dicts under <bronze_root>/<source>/_records/."""
    base = bronze_root / source_name / "_records"
    for rf in sorted(base.glob("**/*.json")):
        try:
            yield json.loads(rf.read_text())
        except (OSError, ValueError):
            continue


def _run_awaiting(params: dict[str, Any]) -> SkillResult:
    """``--awaiting``: embed the corpus's chunks stored without a vector (P0.2).

    The stored text is embedded where an embedder their label admits exists;
    nothing is read from bronze or extracted again. A no-op while none is.
    """
    corpus = params.get("corpus")
    if not corpus:
        return SkillResult(ok=False, errors=["missing required param: corpus"])
    from axiom.rag.ingest import embed_awaiting
    from axiom.rag.store import RAGStore

    from .._dsn import resolve_dsn

    dsn = resolve_dsn(params, env_name=params.get("rag_dsn_env"))
    if not dsn:
        return SkillResult(ok=False, errors=["no RAG DSN (set DP1_RAG_DSN)"])
    store = RAGStore(dsn)
    store.connect()
    try:
        with _authz.action(
            verb="reindex",
            resource=f"rag://{corpus}",
            classification=Classification.INTERNAL,
            actor=params.get("actor"),
        ):
            out = embed_awaiting(store, corpus)
    finally:
        store.close()
    return SkillResult(
        ok=True,
        value=out,
        actions_taken=[
            f"{corpus}: embedded {out['embedded']} chunks; {out['waiting']} still await an admitted embedder"
        ],
    )


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    if params.get("awaiting"):
        return _run_awaiting(params)
    bronze_root = bronze_root_for(params)
    source_name = params.get("source") or os.environ.get("DP1_BOX_SOURCE_NAME", "")
    corpus = params.get("corpus", "rag-community")
    actor = params.get("actor")
    if not source_name:
        return SkillResult(
            ok=False, errors=["missing required param: source (or DP1_BOX_SOURCE_NAME)"]
        )

    # Heavy deps stay function-scoped (mirrors data.ingest): the store +
    # bronze record reconstruction + the embed unit.
    from datetime import UTC, datetime

    from axiom.rag.ingest_router import Disposition
    from axiom.rag.store import RAGStore

    from .._dsn import resolve_dsn
    from ..bronze.router import BronzeWriteResult
    from ..rag_embed import embed_bronze_record
    from ..rag_embed.embedder import extraction_plan, parser_is_current
    from ..sources import FetchedItem

    dsn = resolve_dsn(params, env_name=params.get("rag_dsn_env"))
    if not dsn:
        return SkillResult(ok=False, errors=["no RAG DSN (set DP1_RAG_DSN)"])
    store = RAGStore(dsn)
    store.connect()  # lock-safe schema ensure (bounded lock_timeout)

    # A record is written to the tier bronze stamped on it (``corpus`` only when
    # it has none), so that is the corpus its skip check reads. One indexed read
    # of `documents` per corpus, not a scan of every chunk in every corpus.
    parsers_by_corpus: dict[str, dict[str, dict | None]] = {}

    def target_of(rec: dict[str, Any]) -> str:
        return rec.get("tier") or corpus

    def parsers_of(target: str) -> dict[str, dict | None]:
        if target not in parsers_by_corpus:
            parsers_by_corpus[target] = store.document_parsers(target)
        return parsers_by_corpus[target]

    def is_indexed(rec: dict[str, Any]) -> bool:
        return rec.get("source_path") in parsers_of(target_of(rec))

    def item_of(rec: dict[str, Any]) -> FetchedItem:
        return FetchedItem(
            source_name=rec.get("source_name", source_name),
            item_id=rec["item_id"],
            display_name=rec["display_name"],
            content=b"",
            content_type=rec.get("content_type"),
            size=rec.get("size", 0),
            modified_at=None,
            etag=rec.get("etag"),
            source_path=rec.get("source_path"),
            extra=rec.get("extra", {}) or {},
        )

    def content_of(rec: dict[str, Any]) -> Path:
        sha = rec["content_sha256"]
        return bronze_root / "_content" / sha[:2] / sha

    def is_current(rec: dict[str, Any]) -> bool:
        cp = content_of(rec)
        if not cp.exists():
            return True  # nothing to reprocess from
        try:
            planned = extraction_plan(cp, item_of(rec), sha256=rec["content_sha256"])
        except Exception as exc:  # noqa: BLE001 - an unplannable record keeps what it has
            log.warning("data.reindex: cannot plan %s: %s", rec.get("source_path"), exc)
            return True
        return parser_is_current(parsers_of(target_of(rec)).get(rec["source_path"]), planned)

    def embed_one(rec: dict[str, Any]) -> int:
        sha = rec["content_sha256"]
        cp = content_of(rec)
        if not cp.exists():
            return 0
        item = item_of(rec)
        bw = BronzeWriteResult(
            item_id=rec["item_id"],
            disposition=Disposition.ALLOW,
            tier=target_of(rec),
            content_hash=sha,
            record_path=cp,
            content_path=cp,
            reason=rec.get("reason", ""),
            matched_rule=rec.get("matched_rule"),
            fetched_at=datetime.now(UTC),
            # Declared labels travel on the manifest, so a re-index applies them (#1971).
            access_labels=rec.get("access_labels"),
        )
        stats = embed_bronze_record(bw, item, store)
        return getattr(stats, "chunks_created", 0) if getattr(stats, "indexed", False) else 0

    with _authz.action(
        verb="reindex",
        resource=f"data-platform://bronze/{source_name}",
        classification=Classification.INTERNAL,
        actor=actor,
    ) as act:
        report = reindex_bronze(
            _iter_manifests(bronze_root, source_name),
            already_indexed=set(),
            embed_one=embed_one,
            is_indexed=is_indexed,
            is_current=is_current,
        )

    return SkillResult(
        ok=report.failed == 0,
        actions_taken=[
            f"audit-receipt: {act.receipt_id}",
            f"reindexed {report.indexed} docs ({report.chunks} chunks; "
            f"{report.stale} with a changed extractor), "
            f"skipped {report.skipped}, failed {report.failed}",
        ],
        errors=[f"{sp}: {err}" for sp, err in report.failures[:20]],
    )


__all__ = ["run"]
