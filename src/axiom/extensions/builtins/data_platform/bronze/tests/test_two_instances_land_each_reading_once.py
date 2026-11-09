# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Two instances of one sender, overlapping during an update, land each reading once.

Each instance signs its rows into its own chain, so the chain fields differ for
the same reading. Bronze dedupes on the reading, not the row. Real journals,
real consolidators and the real filesystem sink.
"""

from __future__ import annotations

import json

from axiom.rag.ingest_router import Disposition

from ...bronze import FilesystemTabularBronzeSink, TabularBronzeWriter
from ...daq.consolidator import DAQConsolidator
from ...daq.envelope import ConsolidatedRecord
from ...daq.journal import DAQJournal
from ...identity import RealtimeDedupe, legacy_row_hash, row_identity
from ...ingest_sink import TabularIngestSink
from ...ingest_sink.tabular import PushRowBatch


def _rows(tmp_path, instance: str, readings: range) -> list[dict]:
    j = DAQJournal(tmp_path / f"journal-{instance}")
    cons = DAQConsolidator(journal=j, producer_id=f"daq-loop@site-a#{instance}", feed="loop")
    for n in readings:
        cons.consume(ConsolidatedRecord(schema_id="site-a/epics-v1", ts=f"2026-10-08T00:00:{n // 20:02d}.{(n % 20) * 5:02d}Z", values={"PV1": float(n)}))
    return [rec.to_row() for _, rec in j.read(0, 10_000)]


def _land(sink: TabularIngestSink, rows: list[dict], item: str):
    return sink.ingest_rows("site-a-push", [PushRowBatch(item_id=item, schema_ref="site-a/epics-v1", rows=rows)])


def _sink(tmp_path):
    return TabularIngestSink(writer=TabularBronzeWriter(rules=[], sink=FilesystemTabularBronzeSink(root=tmp_path / "bronze"),
                                                        default_disposition=Disposition.ALLOW, default_tier="rag-org"))


def _landed(tmp_path) -> list[dict]:
    return [json.loads(line)["row"] for p in (tmp_path / "bronze").rglob("_rows/*/*.jsonl") for line in p.read_text().splitlines()]


def test_the_same_reading_from_two_instances_has_one_identity(tmp_path):
    blue, green = _rows(tmp_path, "blue", range(5)), _rows(tmp_path, "green", range(5))
    assert [r["seq"] for r in blue] == [r["seq"] for r in green]  # same seq numbers...
    assert blue[0]["content_hash"] != green[0]["content_hash"]  # ...different chains
    assert [row_identity(r) for r in blue] == [row_identity(r) for r in green]


def test_an_overlap_lands_every_reading_exactly_once(tmp_path):
    sink = _sink(tmp_path)
    blue = _rows(tmp_path, "blue", range(0, 60))      # blue reads 0..59, then stops
    green = _rows(tmp_path, "green", range(40, 100))  # green starts at 40: 20 readings overlap
    r1, r2 = _land(sink, blue, "blue"), _land(sink, green, "green")
    assert (r1.rows_landed, r2.rows_landed, r2.rows_duplicate) == (60, 40, 20)
    values = sorted(r["values"]["PV1"] for r in _landed(tmp_path))
    assert values == [float(n) for n in range(100)]  # no gap, no duplicate


def test_a_reading_seen_before_reading_identity_is_not_landed_again(tmp_path):
    rows = _rows(tmp_path, "blue", range(3))
    seen = tmp_path / "bronze" / "site-a-push" / "_seen.txt"
    seen.parent.mkdir(parents=True)
    seen.write_text("".join(legacy_row_hash(r) + "\n" for r in rows))
    assert _land(_sink(tmp_path), rows, "replay").rows_landed == 0


def test_a_plain_tabular_row_dedupes_as_it_always_did(tmp_path):
    row = {"site": "s", "value": 1}
    assert row_identity(row) == legacy_row_hash(row)


def test_a_realtime_consumer_drops_the_second_copy():
    d = RealtimeDedupe(window=3)
    assert [d.first_time(k) for k in "aabca"] == [True, False, True, True, False]
    assert d.dropped == 2
