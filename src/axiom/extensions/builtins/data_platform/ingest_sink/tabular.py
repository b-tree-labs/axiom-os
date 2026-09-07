# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``TabularIngestSink`` — the row lane's push core, peer to :class:`IngestSink`.

Same push-first model (a thin egress agent POSTs to a central sink), but the
unit is a batch of typed rows, not a byte blob. Each :class:`PushRowBatch` is
shaped into the existing :class:`~..contracts.RowBatch` and driven through the
existing :class:`~..bronze.TabularBronzeWriter` (the same provenance gate + row
``content_hash`` dedup the pull/CDC path uses) — no RAG embed, since the
terminal artifact is rows in a table, not chunks. This is the "shared infra,
two shapes" mirror of the document push path (ADR-079 / ADR-001).
"""

from __future__ import annotations

import json as _json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from axiom.rag.ingest_router import Disposition

from ..bronze import TabularBronzeWriter
from ..contracts import RowBatch
from ..ingest_run import IngestRunReport, RunStore

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PushRowBatch:
    """One tabular batch on a push request, before it becomes a ``RowBatch``.

    The front door hands rows already parsed to JSON objects; the core shapes
    them into a :class:`RowBatch` (content-addressing the canonical JSON as the
    replay/audit payload).
    """

    item_id: str
    schema_ref: str
    rows: list[dict]
    etag: str | None = None
    source_path: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TabularIngestResult:
    """Outcome of one tabular push request (one ``source``, N batches)."""

    source: str
    accepted: int       # batches accepted
    landed: int         # batches that landed >=1 new row
    excluded: int       # batches gated to EXCLUDE
    errored: int
    rows_in: int
    rows_landed: int
    rows_duplicate: int
    funnel: dict | None = None


class TabularIngestSink:
    """Push-ingest core for tabular rows: batches → TabularBronzeWriter (gate +
    row dedup). No embed step — rows land in the typed bronze tree."""

    def __init__(self, *, writer: TabularBronzeWriter) -> None:
        self._writer = writer

    def ingest_rows(
        self,
        source: str,
        batches: Iterable[PushRowBatch],
        *,
        run_store: RunStore | None = None,
    ) -> TabularIngestResult:
        run = IngestRunReport.start("push", source=source, stages=("to_process", "loaded"))
        landed = excluded = errored = 0
        totals = {"rows_in": 0, "rows_landed": 0, "rows_duplicate": 0}

        for pb in batches:
            run.entered("to_process", 1)
            run.advanced("to_process", 1)
            batch = RowBatch(
                source_name=source,
                item_id=pb.item_id,
                etag=pb.etag,
                modified_at=datetime.now(UTC),
                schema_ref=pb.schema_ref,
                rows=list(pb.rows),
                raw=_json.dumps(pb.rows, sort_keys=True, default=str).encode("utf-8"),
                source_path=pb.source_path,
                extra=dict(pb.metadata),
            )
            run.entered("loaded", 1)
            try:
                res = self._writer.write(batch)
            except Exception as exc:  # noqa: BLE001 — one bad batch must not sink the push
                log.warning("tabular push write failed for %s/%s: %s", source, pb.item_id, exc)
                errored += 1
                run.failed("loaded", "write_failed", 1)
                continue

            totals["rows_in"] += res.rows_in
            totals["rows_landed"] += res.rows_landed
            totals["rows_duplicate"] += res.rows_duplicate
            if res.disposition is Disposition.EXCLUDE:
                excluded += 1
                run.dropped("loaded", "excluded", 1)
            elif res.rows_landed > 0:
                landed += 1
                run.advanced("loaded", 1)
            else:
                run.dropped("loaded", "no_new_rows", 1)

        for k, v in totals.items():
            run.set_metric(k, v)
        run.finish(failed=errored > 0 and landed == 0)
        if run_store is not None:
            try:
                run_store.save(run)
            except Exception:  # noqa: BLE001 — telemetry must never sink a run
                log.warning("tabular push run-store save failed for %s", run.run_id)

        return TabularIngestResult(
            source=source,
            accepted=run.stage("to_process").entered if run.stage("to_process") else 0,
            landed=landed,
            excluded=excluded,
            errored=errored,
            rows_in=totals["rows_in"],
            rows_landed=totals["rows_landed"],
            rows_duplicate=totals["rows_duplicate"],
            funnel=run.to_dict(),
        )


__all__ = ["PushRowBatch", "TabularIngestResult", "TabularIngestSink"]
