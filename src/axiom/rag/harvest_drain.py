# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Turn queued intent into retrievable text.

Everything expensive happens here, away from the write that caused it:
embedding calls an external service, and the store write is a transaction of its
own. A save must never wait on either.

The drain is deliberately dull. It reads what is pending, does the work, and
clears the queue only once every intent has been acted on — clearing first would
lose the work on a crash mid-drain, which is the failure the queue exists to
prevent.

A failure on one record does not stop the others. It also does not silently
vanish: the report names what failed, and the queue is left intact when anything
did, so the next drain retries rather than the work being lost to a log line.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axiom.rag.harvest_queue import OP_DELETE, HarvestIntent, clear, read_pending

log = logging.getLogger(__name__)


@dataclass
class DrainReport:
    """What a drain actually did — counts plus the records that failed."""

    upserted: int = 0
    deleted: int = 0
    failed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    def describe(self) -> str:
        base = f"{self.upserted} upserted, {self.deleted} deleted"
        if self.failed:
            return f"{base}, {len(self.failed)} failed: {', '.join(self.failed[:5])}"
        return base


def _chunk_for(intent: HarvestIntent):
    """One card, one chunk.

    A harvested card is a few lines about a single record — splitting it would
    scatter one record's facts across results that each look like a different
    source, which is worse retrieval, not better.
    """
    from axiom.rag.chunker import Chunk

    return Chunk(
        text=intent.text,
        source_path=f"entity://{intent.entity_type}/{intent.entity_id}",
        source_title=intent.title or f"{intent.entity_type} {intent.entity_id}",
        chunk_index=0,
        start_line=1,
        source_type="entity",
    )


def drain(
    queue_path: str | Path,
    store: Any,
    *,
    embedder: Callable[[list[str]], list[list[float]]] | None = None,
    limit: int | None = None,
) -> DrainReport:
    """Apply every pending intent to the store.

    ``store`` is a RAGStore. ``embedder`` turns card text into vectors; without
    one the cards are still indexed for full-text retrieval, which is a
    degraded mode worth having rather than a reason to write nothing.
    """
    report = DrainReport()
    pending = read_pending(queue_path)
    if not pending:
        return report
    if limit is not None:
        pending = pending[:limit]

    for intent in pending:
        try:
            if intent.op == OP_DELETE:
                store.delete_document(
                    f"entity://{intent.entity_type}/{intent.entity_id}",
                    corpus=intent.corpus,
                )
                report.deleted += 1
                continue

            if not intent.text:
                # An upsert with no card would replace a real record with an
                # empty one — worse than skipping it.
                report.failed.append(f"{intent.entity_type}/{intent.entity_id} (no card)")
                continue

            chunk = _chunk_for(intent)
            embeddings = embedder([intent.text]) if embedder else None
            store.upsert_chunks(
                [chunk],
                embeddings=embeddings,
                corpus=intent.corpus,
                owner=intent.owner,
                data_source="harvest",
                source_ref_id=intent.entity_id,
            )
            report.upserted += 1
        except Exception:  # noqa: BLE001 - one record must not stop the rest
            log.exception(
                "harvest failed for %s/%s; it stays queued for the next drain",
                intent.entity_type, intent.entity_id,
            )
            report.failed.append(f"{intent.entity_type}/{intent.entity_id}")

    # Only clear when everything landed. Clearing after partial success would
    # drop the failures silently, and a record that never becomes retrievable
    # is invisible — nobody sees the answer that was not there.
    if report.ok and limit is None:
        clear(queue_path)
    elif report.failed:
        log.error(
            "harvest drain kept the queue: %s. The failed records retry on the "
            "next drain rather than being lost.", report.describe(),
        )
    return report


__all__ = ["DrainReport", "drain"]
