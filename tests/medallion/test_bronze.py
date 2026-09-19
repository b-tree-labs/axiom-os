# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Bronze layer — raw, append-only event landing zone.

Slice 5 (code side): bronze ingests tracing events as raw rows, namespaced
by source and day, with no schema enforcement beyond append-only semantics.
Production backing is SeaweedFS + Iceberg; this is the in-memory version
that everything downstream (silver, gold, RAG) composes against.
"""

from __future__ import annotations


def test_append_and_scan_single_partition() -> None:
    from axiom.medallion import BronzeStore

    store = BronzeStore()
    store.append(source="traces", day="2026-04-13", row={"trace_id": "t1", "score": 0.9})
    store.append(source="traces", day="2026-04-13", row={"trace_id": "t2", "score": 0.8})

    rows = list(store.scan(source="traces", day="2026-04-13"))
    assert len(rows) == 2
    assert rows[0]["trace_id"] == "t1"
    assert rows[1]["trace_id"] == "t2"


def test_partitions_are_isolated() -> None:
    from axiom.medallion import BronzeStore

    store = BronzeStore()
    store.append(source="traces", day="2026-04-13", row={"x": 1})
    store.append(source="traces", day="2026-04-14", row={"x": 2})
    store.append(source="findings", day="2026-04-13", row={"x": 3})

    assert [r["x"] for r in store.scan(source="traces", day="2026-04-13")] == [1]
    assert [r["x"] for r in store.scan(source="traces", day="2026-04-14")] == [2]
    assert [r["x"] for r in store.scan(source="findings", day="2026-04-13")] == [3]


def test_scan_range_across_days() -> None:
    from axiom.medallion import BronzeStore

    store = BronzeStore()
    for day, n in [("2026-04-10", 1), ("2026-04-11", 2), ("2026-04-12", 3)]:
        store.append(source="traces", day=day, row={"n": n})

    rows = list(store.scan_range(source="traces", start_day="2026-04-10", end_day="2026-04-11"))
    assert [r["n"] for r in rows] == [1, 2]


def test_bronze_is_append_only() -> None:
    """Rows are immutable once written. No update/delete API by design."""
    from axiom.medallion import BronzeStore

    store = BronzeStore()
    assert not hasattr(store, "update")
    assert not hasattr(store, "delete")
    assert not hasattr(store, "upsert")


def test_bronze_trace_sink_lands_trace_events() -> None:
    """Adapter: a TraceProvider can write events into bronze at flush()."""
    from axiom.medallion import BronzeStore, BronzeTraceSink

    bronze = BronzeStore()
    sink = BronzeTraceSink(bronze=bronze, day="2026-04-13")

    tid = sink.start_trace("chat.completion", user="@ben")
    sink.log_generation(tid, model="bonsai", prompt="hi", output="hello")
    sink.score(tid, name="faithfulness", value=0.9)
    sink.flush()

    trace_rows = list(bronze.scan(source="traces", day="2026-04-13"))
    gen_rows = list(bronze.scan(source="generations", day="2026-04-13"))
    score_rows = list(bronze.scan(source="scores", day="2026-04-13"))

    assert len(trace_rows) == 1
    assert trace_rows[0]["name"] == "chat.completion"
    assert len(gen_rows) == 1
    assert gen_rows[0]["model"] == "bonsai"
    assert len(score_rows) == 1
    assert score_rows[0]["value"] == 0.9
