# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""``data.reindex`` — offline re-index of already-landed bronze records.

The recovery + maintenance path: when content has already landed in bronze
(via a connector pull) but indexing didn't finish or needs to be rebuilt,
this re-runs OCR/chunk/write over every bronze record without re-fetching
from the source. Wraps :func:`reindex_bronze` (the tested orchestration
core); the embed unit is the same ``embed_bronze_record`` the live pipeline
uses, so reindex and first-pass ingest share one code path.

CLI: ``axi data reindex [--bronze-root P] [--corpus C] [--source NAME]``.
MCP: ``data_reindex`` (status/report; classification-aware).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from axiom.governance.classification import Classification
from axiom.infra.skills import SkillContext, SkillResult

from .. import _authz
from ..orchestration.reindex_bronze import reindex_bronze


def _iter_manifests(bronze_root: Path, source_name: str):
    """Yield bronze manifest dicts under <bronze_root>/<source>/_records/."""
    base = bronze_root / source_name / "_records"
    for rf in sorted(base.glob("**/*.json")):
        try:
            yield json.loads(rf.read_text())
        except (OSError, ValueError):
            continue


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    bronze_root = Path(
        params.get("bronze_root") or os.environ.get("DP1_BRONZE_ROOT", "/var/lib/axiom/bronze")
    )
    source_name = params.get("source") or os.environ.get("DP1_BOX_SOURCE_NAME", "")
    corpus = params.get("corpus", "rag-community")
    actor = params.get("actor")
    if not source_name:
        return SkillResult(ok=False, errors=["missing required param: source (or DP1_BOX_SOURCE_NAME)"])

    # Heavy deps stay function-scoped (mirrors data.ingest): the store +
    # bronze record reconstruction + the embed unit.
    from datetime import UTC, datetime

    from axiom.rag.ingest_router import Disposition
    from axiom.rag.store import RAGStore

    from ..bronze.router import BronzeWriteResult
    from ..rag_embed import embed_bronze_record
    from ..sources import FetchedItem

    dsn = os.environ.get(params.get("rag_dsn_env", "DP1_RAG_DSN")) or os.environ.get("DATABASE_URL")
    if not dsn:
        return SkillResult(ok=False, errors=["no RAG DSN (set DP1_RAG_DSN)"])
    store = RAGStore(dsn)
    store.connect()  # lock-safe schema ensure (bounded lock_timeout)

    # already-indexed source_paths (idempotent / resumable)
    already: set[str] = set()
    with store._cur() as cur:  # noqa: SLF001 — internal read; cheap distinct scan
        cur.execute("SELECT DISTINCT source_path FROM chunks")
        already = {r["source_path"] for r in cur.fetchall()}

    def embed_one(rec: dict[str, Any]) -> int:
        sha = rec["content_sha256"]
        cp = bronze_root / "_content" / sha[:2] / sha
        if not cp.exists():
            return 0
        item = FetchedItem(
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
        bw = BronzeWriteResult(
            item_id=rec["item_id"],
            disposition=Disposition.ALLOW,
            tier=rec.get("tier", corpus),
            content_hash=sha,
            record_path=cp,
            content_path=cp,
            reason=rec.get("reason", ""),
            matched_rule=rec.get("matched_rule"),
            fetched_at=datetime.now(UTC),
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
            already_indexed=already,
            embed_one=embed_one,
        )

    return SkillResult(
        ok=report.failed == 0,
        actions_taken=[
            f"audit-receipt: {act.receipt_id}",
            f"reindexed {report.indexed} docs ({report.chunks} chunks), "
            f"skipped {report.skipped}, failed {report.failed}",
        ],
        errors=[f"{sp}: {err}" for sp, err in report.failures[:20]],
    )


__all__ = ["run"]
