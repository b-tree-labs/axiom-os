# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``run_box_to_rag`` — pure-Python Box → bronze → RAG pass.

This is the *logic* of DP-1's pipeline; Dagster and PLINTH both call
into it. Drivers own the watermark persistence + scheduling; this
function is one synchronous pass given a ``since``.

One embed failure does NOT abort the run — its failure is recorded and
the remaining items continue. The bronze write for the failed item has
already landed (the substrate of record), so a retry only needs to
re-run the embed, not re-fetch from Box.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from axiom.rag.ingest_router import Disposition

from ..bronze import BronzeWriter, BronzeWriteResult
from ..rag_embed import EmbedStats, embed_bronze_record
from ..sources import BoxIngestSource

log = logging.getLogger(__name__)


class _StoreLike(Protocol):
    def upsert_chunks(self, chunks: list[Any], embeddings: list[list[float]] | None = ..., **kwargs: Any) -> None: ...


@dataclass(frozen=True)
class BoxRunReport:
    """End-to-end report for one ``run_box_to_rag`` pass."""

    items_seen: int
    items_landed: int
    items_failed: int
    bronze_results: list[BronzeWriteResult]
    embed_stats: list[EmbedStats] = field(default_factory=list)


def run_box_to_rag(
    *,
    source: BoxIngestSource,
    writer: BronzeWriter,
    store: _StoreLike,
    since: datetime | None = None,
    embed: bool = True,
) -> BoxRunReport:
    """Drive one full Box → bronze → (optional) RAG embed pass.

    Returns a :class:`BoxRunReport` summarizing the pass — the driver
    (Dagster sensor or PLINTH skill) decides what to do with the failure
    counts and bronze results.
    """
    item_ids = source.list_changed(since=since)

    bronze_results: list[BronzeWriteResult] = []
    embed_stats: list[EmbedStats] = []
    landed = 0
    failed = 0

    for item_id in item_ids:
        try:
            fetched = source.fetch(item_id)
        except Exception as exc:
            log.warning("Box fetch failed for %s: %s", item_id, exc)
            failed += 1
            continue

        try:
            result = writer.write(fetched)
            bronze_results.append(result)
        except Exception as exc:
            log.warning("Bronze write failed for %s: %s", item_id, exc)
            failed += 1
            continue

        if not embed:
            if result.disposition is Disposition.ALLOW:
                landed += 1
            continue

        # ALLOW only — QUARANTINE waits for review; EXCLUDE has no content.
        stats = embed_bronze_record(result, fetched, store)
        embed_stats.append(stats)
        if stats.indexed:
            landed += 1
        elif stats.skipped_reason == "embed_failed":
            failed += 1
        # quarantine / exclude skips are not failures — they're expected.

    return BoxRunReport(
        items_seen=len(item_ids),
        items_landed=landed,
        items_failed=failed,
        bronze_results=bronze_results,
        embed_stats=embed_stats,
    )


__all__ = ["BoxRunReport", "run_box_to_rag"]
