# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``BronzeWriter`` — provenance-gated dispatcher onto a :class:`BronzeSink`.

For each :class:`FetchedItem` the writer:

1. Builds a relative path (``source_path`` if present, else
   ``<source_name>/<item_id>``).
2. Routes through :func:`axiom.rag.ingest_router.route_path` — that's
   the v0.22.0 gate. ``ALLOW`` / ``QUARANTINE`` / ``EXCLUDE``.
3. EXCLUDE → writes a *decision record only* via the sink. The bytes
   never land.
4. ALLOW / QUARANTINE → writes the content blob (content-addressed,
   sha256) + sidecar manifest via the sink.

The writer never talks to a specific storage backend — sinks own that.
``FilesystemBronzeSink`` is the lean default; the Iceberg sink lives in
the ``[heavy]`` extra and is wired by the Dagster asset (Slice 3).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from axiom.rag.ingest_router import Disposition, ProvenanceRule, RouteDecision, route_path

from ..contracts import FetchedItem, RowBatch


@dataclass(frozen=True)
class BronzeWriteResult:
    """What the writer hands back after one item passes through the gate.

    ``content_hash`` and ``content_path`` are ``None`` for EXCLUDE
    (decision is recorded but no content lands).
    """

    item_id: str
    disposition: Disposition
    tier: str | None
    content_hash: str | None
    record_path: Path
    content_path: Path | None
    reason: str
    matched_rule: str | None
    fetched_at: datetime


class BronzeSink(Protocol):
    """The storage-backend seam the writer dispatches to."""

    def write_record(
        self,
        *,
        item: FetchedItem,
        decision: RouteDecision,
        tier: str | None,
        content_hash: str | None,
        fetched_at: datetime,
    ) -> Path:
        """Persist the sidecar manifest and return its path."""
        ...

    def write_content(self, *, content: bytes, content_hash: str) -> Path:
        """Persist the content-addressed blob and return its path."""
        ...


@dataclass(frozen=True)
class TabularWriteResult:
    """What the tabular writer hands back after one :class:`RowBatch` passes the
    gate — peer to :class:`BronzeWriteResult` for the row lane (ADR-001).

    ``rows_in`` entered; ``rows_landed`` were new (or changed) and written;
    ``rows_duplicate`` were dropped by the row-level ``content_hash`` dedup tier.
    ``rows_landed + rows_duplicate`` reconciles to ``rows_in`` for ALLOW /
    QUARANTINE; EXCLUDE lands nothing (``rows_landed == 0``).
    """

    item_id: str
    disposition: Disposition
    tier: str | None
    rows_in: int
    rows_landed: int
    rows_duplicate: int
    fetched_at: datetime


class TabularBronzeSink(Protocol):
    """Storage seam for a tabular :class:`RowBatch` — peer to :class:`BronzeSink`.

    Same posture as :class:`BronzeSink` (the writer dispatches; the sink owns the
    backend), but the unit is rows, not a content blob. The lands-in-a-typed-
    table implementation is P1 (ADR-001 D3); this declares the seam so the
    ``shape``-switched writer has a stable interface to target.
    """

    def write_rows(
        self,
        *,
        batch: RowBatch,
        decision: RouteDecision,
        tier: str | None,
        fetched_at: datetime,
    ) -> TabularWriteResult:
        """Land ``batch``'s rows (dedup by row ``content_hash``); return counts."""
        ...


class BronzeWriter:
    """Compose the provenance gate with a :class:`BronzeSink`."""

    def __init__(
        self,
        *,
        rules: list[ProvenanceRule],
        sink: BronzeSink,
        default_disposition: Disposition,
        default_tier: str | None,
    ) -> None:
        self._rules = list(rules)
        self._sink = sink
        self._default_disposition = default_disposition
        self._default_tier = default_tier

    def write(self, item: FetchedItem) -> BronzeWriteResult:
        rel_path = item.source_path or f"{item.source_name}/{item.item_id}"
        decision = route_path(
            rel_path,
            self._rules,
            default_disposition=self._default_disposition,
            default_tier=self._default_tier,
        )

        # Tier resolution: an ALLOW rule may omit tier; fall back to default.
        tier = decision.tier if decision.tier is not None else self._default_tier

        fetched_at = datetime.now(UTC)

        if decision.disposition is Disposition.EXCLUDE:
            # Decision is recorded but no content lands.
            record_path = self._sink.write_record(
                item=item,
                decision=decision,
                tier=tier,
                content_hash=None,
                fetched_at=fetched_at,
            )
            return BronzeWriteResult(
                item_id=item.item_id,
                disposition=decision.disposition,
                tier=tier,
                content_hash=None,
                record_path=record_path,
                content_path=None,
                reason=decision.reason,
                matched_rule=decision.matched,
                fetched_at=fetched_at,
            )

        # ALLOW or QUARANTINE — content + sidecar.
        content_hash = hashlib.sha256(item.content).hexdigest()
        content_path = self._sink.write_content(content=item.content, content_hash=content_hash)
        record_path = self._sink.write_record(
            item=item,
            decision=decision,
            tier=tier,
            content_hash=content_hash,
            fetched_at=fetched_at,
        )
        return BronzeWriteResult(
            item_id=item.item_id,
            disposition=decision.disposition,
            tier=tier,
            content_hash=content_hash,
            record_path=record_path,
            content_path=content_path,
            reason=decision.reason,
            matched_rule=decision.matched,
            fetched_at=fetched_at,
        )


class TabularBronzeWriter:
    """Compose the provenance gate with a :class:`TabularBronzeSink` — the row
    lane's peer to :class:`BronzeWriter` (ADR-001).

    Same gate, same rules: a :class:`RowBatch` is classified ALLOW / QUARANTINE
    / EXCLUDE by its source path, exactly as a :class:`FetchedItem` is. The
    divergence is only the unit — the sink lands *rows* (with per-row
    ``content_hash`` dedup) instead of a content blob. EXCLUDE handling lives in
    the sink (a decision log, no rows), mirroring the document lane.
    """

    def __init__(
        self,
        *,
        rules: list[ProvenanceRule],
        sink: TabularBronzeSink,
        default_disposition: Disposition,
        default_tier: str | None,
    ) -> None:
        self._rules = list(rules)
        self._sink = sink
        self._default_disposition = default_disposition
        self._default_tier = default_tier

    def write(self, batch: RowBatch) -> TabularWriteResult:
        rel_path = batch.source_path or f"{batch.source_name}/{batch.item_id}"
        decision = route_path(
            rel_path,
            self._rules,
            default_disposition=self._default_disposition,
            default_tier=self._default_tier,
        )
        tier = decision.tier if decision.tier is not None else self._default_tier
        return self._sink.write_rows(
            batch=batch,
            decision=decision,
            tier=tier,
            fetched_at=datetime.now(UTC),
        )


__all__ = [
    "BronzeSink",
    "BronzeWriteResult",
    "BronzeWriter",
    "TabularBronzeSink",
    "TabularBronzeWriter",
    "TabularWriteResult",
]
