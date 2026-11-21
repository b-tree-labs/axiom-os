# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TabularIngestSink tests — the row lane's push core.

These exercise push → gate → row landing → dedup → funnel with the real
:class:`TabularBronzeWriter` + :class:`FilesystemTabularBronzeSink` over a tmp
bronze root (no Postgres, no network, no bound port), mirroring the document
lane's test setup in ``test_ingest_sink.py``.
"""

from __future__ import annotations

from axiom.rag.ingest_router import Disposition, ProvenanceRule

from ...bronze import FilesystemTabularBronzeSink, TabularBronzeWriter
from ...ingest_sink import PushRowBatch, TabularIngestSink


def _writer(tmp_path, rules=None, default=Disposition.ALLOW):
    return TabularBronzeWriter(
        rules=rules or [],
        sink=FilesystemTabularBronzeSink(root=tmp_path / "bronze"),
        default_disposition=default,
        default_tier="rag-community",
    )


def _batch(item_id="batch-1", rows=None, **kw):
    return PushRowBatch(
        item_id=item_id,
        schema_ref="test/rows-v1",
        rows=rows if rows is not None else [{"a": 1}, {"a": 2}],
        **kw,
    )


def test_push_lands_rows(tmp_path):
    sink = TabularIngestSink(writer=_writer(tmp_path))
    result = sink.ingest_rows("unit-src", [_batch()])
    assert (result.accepted, result.landed, result.excluded, result.errored) == (1, 1, 0, 0)
    assert result.rows_in == 2
    assert result.rows_landed == 2
    assert result.rows_duplicate == 0
    rows_dir = tmp_path / "bronze" / "unit-src" / "_rows"
    assert list(rows_dir.rglob("*.jsonl")), "ALLOW rows must land under _rows/"


def test_row_dedup_across_pushes(tmp_path):
    writer = _writer(tmp_path)
    sink = TabularIngestSink(writer=writer)
    sink.ingest_rows("unit-src", [_batch()])
    result = sink.ingest_rows("unit-src", [_batch(item_id="batch-2")])
    assert result.rows_in == 2
    assert result.rows_landed == 0
    assert result.rows_duplicate == 2
    assert result.landed == 0  # no NEW rows → the batch does not count as landed


def test_exclude_gated_batch_lands_nothing(tmp_path):
    rules = [ProvenanceRule(pattern="secret/*", disposition=Disposition.EXCLUDE)]
    sink = TabularIngestSink(writer=_writer(tmp_path, rules=rules))
    result = sink.ingest_rows(
        "unit-src", [_batch(source_path="secret/patients.csv")]
    )
    assert result.excluded == 1
    assert result.landed == 0
    assert result.rows_landed == 0
    assert not list((tmp_path / "bronze" / "unit-src").rglob("_rows/**/*.jsonl"))


def test_one_bad_batch_does_not_sink_the_push(tmp_path):
    real = _writer(tmp_path)

    class PoisonWriter:
        def write(self, batch):
            if batch.item_id == "poison":
                raise RuntimeError("boom")
            return real.write(batch)

    sink = TabularIngestSink(writer=PoisonWriter())
    result = sink.ingest_rows(
        "unit-src",
        [_batch(item_id="poison"), _batch(item_id="good", rows=[{"b": 9}])],
    )
    assert result.errored == 1
    assert result.landed == 1
    assert result.rows_landed == 1


def test_run_store_saved_and_failure_isolated(tmp_path):
    saved = []

    class ListStore:
        def save(self, report):
            saved.append(report)

    sink = TabularIngestSink(writer=_writer(tmp_path))
    sink.ingest_rows("unit-src", [_batch()], run_store=ListStore())
    assert len(saved) == 1
    assert saved[0].to_dict()["metrics"]["rows_landed"] == 2

    class BrokenStore:
        def save(self, report):
            raise OSError("disk gone")

    # Telemetry must never sink a push.
    result = sink.ingest_rows(
        "unit-src", [_batch(item_id="b2", rows=[{"c": 3}])], run_store=BrokenStore()
    )
    assert result.landed == 1


def test_funnel_reports_row_metrics(tmp_path):
    sink = TabularIngestSink(writer=_writer(tmp_path))
    result = sink.ingest_rows("unit-src", [_batch()])
    assert result.funnel is not None
    assert result.funnel["metrics"] == {
        "rows_in": 2,
        "rows_landed": 2,
        "rows_duplicate": 0,
    }


def test_canonical_raw_is_key_order_independent(tmp_path):
    """Two pushes whose rows differ only in dict key order dedup identically —
    the canonical (sorted-keys) raw payload is what gets content-addressed."""
    sink = TabularIngestSink(writer=_writer(tmp_path))
    sink.ingest_rows("unit-src", [_batch(rows=[{"x": 1, "y": 2}])])
    result = sink.ingest_rows(
        "unit-src", [_batch(item_id="b2", rows=[{"y": 2, "x": 1}])]
    )
    assert result.rows_duplicate == 1
    assert result.rows_landed == 0
