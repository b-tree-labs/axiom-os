# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the tabular bronze lane (ADR-001 P1): TabularBronzeWriter routes a
RowBatch through the SAME provenance gate as the document lane, then dispatches
to FilesystemTabularBronzeSink, which lands typed rows with per-row content_hash
dedup. EXCLUDE lands no rows (decision log only); dedup persists across runs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from axiom.rag.ingest_router import Disposition


def _writer(root: Path, disposition: Disposition = Disposition.ALLOW):
    from axiom.extensions.builtins.data_platform.bronze import (
        FilesystemTabularBronzeSink,
        TabularBronzeWriter,
    )

    return TabularBronzeWriter(
        rules=[],
        sink=FilesystemTabularBronzeSink(root=root),
        default_disposition=disposition,
        default_tier="rag-community",
    )


def _batch(rows, *, item_id="batch-1", source_name="metrics-feed"):
    from axiom.extensions.builtins.data_platform.contracts import RowBatch

    raw = ("\n".join(str(r) for r in rows)).encode()
    return RowBatch(
        source_name=source_name,
        item_id=item_id,
        etag=None,
        modified_at=datetime(2026, 4, 1, tzinfo=UTC),
        schema_ref="metrics.series.v1",
        rows=rows,
        raw=raw,
    )


# ---- dedup ----------------------------------------------------------------


def test_first_batch_lands_all_rows(tmp_path: Path):
    w = _writer(tmp_path)
    res = w.write(_batch([{"d": "2026-04-01", "v": 1.0}, {"d": "2026-04-02", "v": 2.0}]))
    assert res.rows_in == 2 and res.rows_landed == 2 and res.rows_duplicate == 0
    jsonl = list((tmp_path / "metrics-feed" / "_rows").rglob("*.jsonl"))
    assert len(jsonl) == 1
    assert len(jsonl[0].read_text().strip().splitlines()) == 2


def test_reland_same_batch_is_all_duplicate(tmp_path: Path):
    w = _writer(tmp_path)
    rows = [{"d": "2026-04-01", "v": 1.0}, {"d": "2026-04-02", "v": 2.0}]
    w.write(_batch(rows))
    res = w.write(_batch(rows))               # identical → nothing new lands
    assert res.rows_landed == 0 and res.rows_duplicate == 2


def test_changed_cell_lands_only_the_new_row(tmp_path: Path):
    w = _writer(tmp_path)
    w.write(_batch([{"d": "2026-04-01", "v": 1.0}]))
    res = w.write(_batch([{"d": "2026-04-01", "v": 1.0}, {"d": "2026-04-02", "v": 2.0}]))
    assert res.rows_landed == 1 and res.rows_duplicate == 1


def test_same_key_different_value_is_a_new_row(tmp_path: Path):
    # content_hash is over the whole row, so a corrected value is a NEW row
    # (bronze keeps both; SCD-2 supersession is P3's job, not dedup's).
    w = _writer(tmp_path)
    w.write(_batch([{"d": "2026-04-01", "v": 1.0}]))
    res = w.write(_batch([{"d": "2026-04-01", "v": 1.5}]))
    assert res.rows_landed == 1 and res.rows_duplicate == 0


def test_dedup_persists_across_sink_instances(tmp_path: Path):
    from axiom.extensions.builtins.data_platform.bronze import (
        FilesystemTabularBronzeSink,
        TabularBronzeWriter,
    )

    rows = [{"d": "2026-04-01", "v": 1.0}]
    _writer(tmp_path).write(_batch(rows))
    # A fresh writer+sink on the SAME root must re-read the _seen ledger.
    w2 = TabularBronzeWriter(
        rules=[],
        sink=FilesystemTabularBronzeSink(root=tmp_path),
        default_disposition=Disposition.ALLOW,
        default_tier="rag-community",
    )
    res = w2.write(_batch(rows))
    assert res.rows_landed == 0 and res.rows_duplicate == 1


# ---- disposition gate -----------------------------------------------------


def test_exclude_lands_no_rows_but_logs_decision(tmp_path: Path):
    w = _writer(tmp_path, disposition=Disposition.EXCLUDE)
    res = w.write(_batch([{"d": "2026-04-01", "v": 1.0}]))
    assert res.disposition is Disposition.EXCLUDE
    assert res.rows_landed == 0 and res.rows_duplicate == 0
    assert not (tmp_path / "metrics-feed" / "_rows").exists()          # no rows landed
    assert list((tmp_path / "metrics-feed" / "_excluded").rglob("*.json"))  # decision logged


def test_quarantine_lands_rows_in_quarantine_dir(tmp_path: Path):
    w = _writer(tmp_path, disposition=Disposition.QUARANTINE)
    res = w.write(_batch([{"d": "2026-04-01", "v": 1.0}]))
    assert res.disposition is Disposition.QUARANTINE and res.rows_landed == 1
    assert list((tmp_path / "metrics-feed" / "_quarantine_rows").rglob("*.jsonl"))
    assert not (tmp_path / "metrics-feed" / "_rows").exists()


def test_raw_payload_is_content_addressed(tmp_path: Path):
    w = _writer(tmp_path)
    w.write(_batch([{"d": "2026-04-01", "v": 1.0}]))
    blobs = list((tmp_path / "metrics-feed" / "_content").rglob("*"))
    assert any(b.is_file() for b in blobs)
