# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Retention deletes a collector's rows file, not only its content blob.

A collector names its batches ``<producer>/<feed>/<first>-<last>``. The bronze
sink keeps the rows under a filename-safe rendering of that id, and retention
deleted by globbing the raw id, which matched nothing. So every pull-side and
archive-side retention pass deleted the content blob and left the larger rows
file behind, reported the batch deleted, and freed about a third of what it
said. Found by the full-disk proof: reclaim ran, said it had rotated three
batches out, and the alarm stayed up.
"""

from __future__ import annotations

from pathlib import Path

from axiom.rag.ingest_router import Disposition

from ...bronze import FilesystemTabularBronzeSink, TabularBronzeWriter
from ...ingest_sink import PushRowBatch, TabularIngestSink
from ...ingest_sink.edge import EdgeOutbox
from ...ingest_sink.edge_retention import EdgeAcks, prune

SOURCE = "site-a-src"


def _land(root: Path, outbox: EdgeOutbox, item_id: str) -> None:
    sink = TabularIngestSink(
        writer=TabularBronzeWriter(rules=[], sink=FilesystemTabularBronzeSink(root=root),
                                   default_disposition=Disposition.ALLOW, default_tier="restricted"),
        on_landed=outbox.record,
    )
    # Distinct per item: the sink lands a batch by content hash, so two batches
    # with the same rows are one batch.
    rows = [{"channel": "TC1", "ts": f"2026-10-08T00:00:{i:02d}Z", "value": float(i), "item": item_id}
            for i in range(5)]
    sink.ingest_rows(SOURCE, [PushRowBatch(item_id=item_id, schema_ref="s/v1", rows=rows,
                                           metadata={"site": "site-a"})])


def _files(root: Path, sub: str) -> list[Path]:
    return sorted(p for p in (root / SOURCE / sub).rglob("*") if p.is_file())


def test_a_slash_bearing_item_id_loses_its_rows_file_and_its_blob(tmp_path):
    bronze, outbox = tmp_path / "bronze", EdgeOutbox(tmp_path / "outbox")
    _land(bronze, outbox, "site-a/loop/0-1999")  # what a collector sends
    _land(bronze, outbox, "run-7")  # what a file drop sends
    assert len(_files(bronze, "_rows")) == 2 and len(_files(bronze, "_content")) == 2

    acks = EdgeAcks(outbox.dir)
    acks.ack("@down:org", 2)
    result = prune(outbox, acks, downstreams=["@down:org"], bronze_root_for=lambda _s: bronze, min_age_hours=0)

    assert result["deleted_batches"] == 2
    assert _files(bronze, "_rows") == [] and _files(bronze, "_content") == []
