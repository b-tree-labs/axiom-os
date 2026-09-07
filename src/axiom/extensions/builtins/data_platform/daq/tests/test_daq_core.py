# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The DAQ producer core against spec-signal-ingest-and-producer §11.

Journal-first ordering, restart resume, cursor isolation, credited trip,
stuck value, overflow gating, chain integrity, at-least-once delivery, and
"site never in the payload" — each obligation is one test below.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ..consolidator import DAQConsolidator
from ..dispatcher import LiveDispatcher
from ..envelope import ConsolidatedRecord, JournaledRecord, SignalEnvelope, verify_chain
from ..health import CreditedGuard, silence
from ..journal import DAQJournal, JournalFull, OverflowPolicy, validate_overflow_policy
from ..producer import Producer
from ..transmitter import DAQTransmitter


def _rec(i: int, **values):
    return ConsolidatedRecord(
        schema_id="unit/sample-v1",
        ts=f"2026-01-01T00:00:{i:02d}Z",
        values=values or {"x": i},
    )


def _cons(journal, **kw):
    kw.setdefault("producer_id", "p1")
    kw.setdefault("stream", "s1")
    return DAQConsolidator(journal=journal, **kw)


# ---------------------------------------------------------------- chain


def test_chain_is_gapless_and_tamper_evident(tmp_path):
    j = DAQJournal(tmp_path)
    c = _cons(j)
    for i in range(5):
        c.consume(_rec(i))
    recs = list(j.iter_all())
    assert [r.envelope.seq for r in recs] == [0, 1, 2, 3, 4]
    assert recs[0].envelope.prev_hash is None
    assert all(recs[i].envelope.prev_hash == recs[i - 1].envelope.content_hash for i in range(1, 5))
    assert verify_chain(recs) == []
    # a mutated record breaks its own hash; a missing seq is a gap
    mutated = JournaledRecord(envelope=recs[2].envelope, record=_rec(2, x=999))
    faults = verify_chain([recs[0], recs[1], mutated, recs[4]])
    kinds = {(f.seq, f.kind) for f in faults}
    assert (2, "mutated") in kinds and (4, "gap") in kinds


def test_content_hash_covers_scale_factor():
    """Same numbers, different scale factor → different record (spec §3)."""
    a = _rec(0, value=1.0, scale=1.0)
    b = _rec(0, value=1.0, scale=1000.0)
    assert a.record_hash() != b.record_hash()


def test_envelope_validates_classification():
    with pytest.raises(ValueError, match="model_ref"):
        SignalEnvelope(
            producer_id="p",
            stream="s",
            seq=0,
            prev_hash=None,
            content_hash="sha256:0",
            source_class="predicted",
        )
    with pytest.raises(ValueError):
        SignalEnvelope(
            producer_id="p",
            stream="s",
            seq=0,
            prev_hash=None,
            content_hash="sha256:0",
            delivery_class="urgent",
        )


# ---------------------------------------------------------------- journal + restart


def test_restart_resumes_seq_and_chain_without_gap_or_duplicate(tmp_path):
    j = DAQJournal(tmp_path, segment_records=2)
    c = _cons(j)
    for i in range(3):
        c.consume(_rec(i))
    last_hash = j.tail().envelope.content_hash
    # "restart": new Journal + Consolidator over the same directory
    j2 = DAQJournal(tmp_path, segment_records=2)
    c2 = _cons(j2)
    assert c2.next_seq == 3
    out = c2.consume(_rec(3))
    assert out.envelope.seq == 3 and out.envelope.prev_hash == last_hash
    assert verify_chain(list(j2.iter_all())) == []
    assert j2.end == 4 and len(list(j2.iter_all())) == 4


def test_two_streams_share_one_journal_with_independent_chains(tmp_path):
    j = DAQJournal(tmp_path)
    a, b = _cons(j, stream="a"), _cons(j, stream="b")
    a.consume(_rec(0))
    b.consume(_rec(0))
    a.consume(_rec(1))
    assert [r.envelope.seq for r in j.iter_all() if r.envelope.stream == "a"] == [0, 1]
    assert [r.envelope.seq for r in j.iter_all() if r.envelope.stream == "b"] == [0]
    assert _cons(DAQJournal(tmp_path), stream="b").next_seq == 1


def test_cursors_persist_and_report_lag(tmp_path):
    j = DAQJournal(tmp_path)
    c = _cons(j)
    for i in range(4):
        c.consume(_rec(i))
    assert j.cursor("tx") == 0 and j.lag("tx") == 4
    j.commit("tx", 3)
    assert DAQJournal(tmp_path).cursor("tx") == 3
    assert DAQJournal(tmp_path).lag("tx") == 1
    assert j.read(3, 10)[0][1].envelope.seq == 3
    with pytest.raises(ValueError):
        j.cursor("../escape")


# ---------------------------------------------------------------- overflow gating


def test_overflow_policy_is_gated_by_sensitivity_and_delivery_class():
    for s in ("ec-controlled", "itar"):
        with pytest.raises(ValueError, match="drop_oldest"):
            validate_overflow_policy(
                OverflowPolicy.DROP_OLDEST, sensitivity=s, delivery_class="standard"
            )
    with pytest.raises(ValueError, match="block_producer"):
        validate_overflow_policy(
            OverflowPolicy.BLOCK_PRODUCER, sensitivity="open", delivery_class="credited"
        )
    with pytest.raises(ValueError, match="trip_on_gap"):
        validate_overflow_policy(
            OverflowPolicy.TRIP_ON_GAP, sensitivity="open", delivery_class="standard"
        )
    assert validate_overflow_policy(
        OverflowPolicy.DROP_OLDEST, sensitivity="internal", delivery_class="standard"
    )
    assert validate_overflow_policy(
        OverflowPolicy.TRIP_ON_GAP, sensitivity="itar", delivery_class="credited"
    )


def test_block_producer_raises_and_the_producer_reports_backpressure(tmp_path):
    j = DAQJournal(tmp_path, max_bytes=600, policy=OverflowPolicy.BLOCK_PRODUCER)
    c = _cons(j, sensitivity="ec-controlled")
    with pytest.raises(JournalFull):
        for i in range(50):
            c.consume(_rec(i))
    assert 0 < j.end < 50


def test_drop_oldest_keeps_the_newest_and_counts_the_drop(tmp_path):
    j = DAQJournal(tmp_path, max_bytes=1500, policy=OverflowPolicy.DROP_OLDEST, segment_records=2)
    c = _cons(j, sensitivity="internal")
    for i in range(20):
        c.consume(_rec(i))
    kept = [r.envelope.seq for r in j.iter_all()]
    assert kept and kept[-1] == 19 and kept[0] > 0
    assert j.dropped == 20 - len(kept)
    assert j.head == kept[0]


def test_trip_on_gap_never_blocks_and_leaves_a_visible_seq_gap(tmp_path):
    j = DAQJournal(tmp_path, max_bytes=900, policy=OverflowPolicy.TRIP_ON_GAP)
    c = _cons(j, delivery_class="credited")
    outcomes = [c.consume(_rec(i)) for i in range(12)]  # never raises
    assert any(o is None for o in outcomes) and j.trips > 0
    seqs = [r.envelope.seq for r in j.iter_all()]
    assert seqs == list(range(len(seqs)))  # stored ones are contiguous...
    assert c.next_seq == 12  # ...but the seq kept advancing: the gap is the marker
    guard = CreditedGuard(deadline_s=3600)
    for r in j.iter_all():
        guard.observe(r.envelope, r.record.ts, now=datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC))
    # a later record after the gap trips the credited consumer
    late = SignalEnvelope(
        producer_id="p1",
        stream="s1",
        seq=c.next_seq,
        prev_hash="x",
        content_hash="y",
        delivery_class="credited",
    )
    assert guard.observe(
        late, "2026-01-01T00:00:12Z", now=datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)
    )
    assert any("seq gap" in t for t in guard.trips)


# ---------------------------------------------------------------- credited + stuck + silence


def test_credited_guard_trips_on_staleness_and_stays_tripped():
    g = CreditedGuard(deadline_s=2.0)
    e = SignalEnvelope(
        producer_id="p",
        stream="s",
        seq=0,
        prev_hash=None,
        content_hash="h",
        delivery_class="credited",
    )
    now = datetime(2026, 1, 1, 0, 0, 10, tzinfo=UTC)
    assert not g.observe(e, "2026-01-01T00:00:09Z", now=now)
    e1 = SignalEnvelope(
        producer_id="p",
        stream="s",
        seq=1,
        prev_hash="h",
        content_hash="i",
        delivery_class="credited",
    )
    assert g.observe(e1, "2026-01-01T00:00:05Z", now=now)  # 5s old > 2s deadline
    e2 = SignalEnvelope(
        producer_id="p",
        stream="s",
        seq=2,
        prev_hash="i",
        content_hash="j",
        delivery_class="credited",
    )
    assert g.observe(e2, "2026-01-01T00:00:10Z", now=now)  # fresh, still tripped
    g.reset()
    assert not g.tripped


def test_stuck_value_is_detected_with_advancing_seq(tmp_path):
    j = DAQJournal(tmp_path)
    c = _cons(j, stuck_limit=3)
    for i in range(5):
        # a fresh receive timestamp every time — the reading itself never moves
        c.consume(
            ConsolidatedRecord(schema_id="s", ts=f"2026-01-01T00:00:{i:02d}Z", values={"x": 1})
        )
    assert c.next_seq == 5 and c.stuck_events >= 1
    assert c.health_details()["stuck_run"] == 4
    # the moment the reading changes, the run resets
    c.consume(ConsolidatedRecord(schema_id="s", ts="2026-01-01T00:01:00Z", values={"x": 2}))
    assert c.health_details()["stuck_run"] == 0
    a = ConsolidatedRecord(schema_id="s", ts="t1", values={"x": 1})
    b = ConsolidatedRecord(schema_id="s", ts="t2", values={"x": 1})
    assert a.record_hash() == b.record_hash()
    assert (
        a.record_hash()
        != ConsolidatedRecord(schema_id="s", ts="t1", values={"x": 1}, quality="bad").record_hash()
    )


def test_silence_detection():
    now = datetime(2026, 1, 1, 0, 1, 0, tzinfo=UTC)
    assert silence(None, expected_interval_s=1.0, now=now).silent
    assert not silence("2026-01-01T00:00:59Z", expected_interval_s=1.0, now=now).silent
    v = silence("2026-01-01T00:00:00Z", expected_interval_s=1.0, factor=3.0, now=now)
    assert v.silent and v.silent_for_s == 60.0


# ---------------------------------------------------------------- transmitter


class _FakeTransport:
    def __init__(self, script):
        self.script = list(script)  # each: int status | 'boom' | (status, text)
        self.requests = []

    def post(self, url, body, headers):
        self.requests.append((url, json.loads(body), headers))
        step = self.script.pop(0) if self.script else 200
        if step == "boom":
            raise OSError("connection refused")
        if isinstance(step, tuple):
            return step
        return step, json.dumps({"accepted": 1, "landed": 1})


def _tx(j, transport, **kw):
    clock = [0.0]
    kw.setdefault("batch_size", 100)
    tx = DAQTransmitter(
        journal=j,
        face_url="http://face.example",
        source="unit-src",
        schema_ref="unit/sample-v1",
        token="axk_test",
        transport=transport,
        backoff=(5, 10),
        clock=lambda: clock[0],
        **kw,
    )
    return tx, clock


def test_transmitter_is_at_least_once_and_replays_after_failure(tmp_path):
    j = DAQJournal(tmp_path)
    c = _cons(j)
    for i in range(3):
        c.consume(_rec(i))
    ft = _FakeTransport(["boom", (503, "down"), 200])
    tx, clock = _tx(j, ft)
    r1 = tx.pump()
    assert (
        r1.sent == 0 and r1.deferred == 3 and r1.retry_after_s == 5 and tx.connection == "degraded"
    )
    assert tx.pump().retry_after_s is not None and len(ft.requests) == 1  # backoff window holds
    clock[0] = 6
    r2 = tx.pump()
    assert r2.deferred == 3 and r2.last_status == 503 and r2.retry_after_s == 10
    clock[0] = 20
    r3 = tx.pump()
    assert r3.sent == 3 and tx.connection == "ok" and j.cursor("transmitter") == 3
    # the same batch was posted each time (replay) — the face's dedup lands it once
    bodies = [req[1] for req in ft.requests]
    assert len(bodies) == 3 and bodies[0] == bodies[2]
    assert bodies[0]["batches"][0]["item_id"] == "p1/s1/0-2"
    assert ft.requests[-1][2]["Authorization"] == "Bearer axk_test"
    assert tx.pump().sent == 0  # drained


def test_transmitter_dead_letters_a_definitive_refusal_and_moves_on(tmp_path):
    j = DAQJournal(tmp_path)
    c = _cons(j)
    for i in range(2):
        c.consume(_rec(i))
    tx, _ = _tx(j, _FakeTransport([(403, "payload site is not the credential's site")]))
    r = tx.pump()
    assert r.refused == 2 and r.sent == 0 and j.cursor("transmitter") == 2
    lines = [json.loads(x) for x in (tmp_path / "deadletter.jsonl").read_text().splitlines()]
    assert [x["seq"] for x in lines] == [0, 1] and lines[0]["status"] == 403
    assert tx.health_details()["refused_total"] == 2


def test_transmitter_never_carries_site_and_stamps_classification(tmp_path):
    j = DAQJournal(tmp_path)
    c = _cons(j, source_class="predicted", model_ref="model-a@1.2")
    c.consume(_rec(0))
    with pytest.raises(ValueError, match="site"):
        _tx(j, _FakeTransport([]), extra_metadata={"site": "alpha"})
    ft = _FakeTransport([200])
    tx, _ = _tx(j, ft, extra_metadata={"unit": "kW"})
    tx.pump()
    body = ft.requests[0][1]
    batch = body["batches"][0]
    assert "site" not in batch["metadata"] and "site" not in json.dumps(body)
    assert batch["metadata"]["source_class"] == "predicted"
    assert batch["metadata"]["model_ref"] == "model-a@1.2"
    assert batch["metadata"]["unit"] == "kW"
    row = batch["rows"][0]
    assert row["seq"] == 0 and row["schema_id"] == "unit/sample-v1" and row["values"] == {"x": 0}


def test_transmitter_schema_ref_per_stream_and_batches_never_mix_streams(tmp_path):
    j = DAQJournal(tmp_path)
    a, b = _cons(j, stream="scalars"), _cons(j, stream="grid")
    a.consume(_rec(0))
    b.consume(_rec(0, v=[1, 2]))
    a.consume(_rec(1))
    ft = _FakeTransport([200])
    tx = DAQTransmitter(
        journal=j,
        face_url="http://f",
        source="s",
        schema_ref={"scalars": "x/scalars-v1", "*": "x/other-v1"},
        transport=ft,
    )
    assert tx.pump().sent == 3
    batches = {b["metadata"]["stream"]: b for b in ft.requests[0][1]["batches"]}
    assert batches["scalars"]["schema_ref"] == "x/scalars-v1"
    assert batches["grid"]["schema_ref"] == "x/other-v1"
    assert batches["scalars"]["item_id"] == "p1/scalars/0-1"
    assert [r["seq"] for r in batches["scalars"]["rows"]] == [0, 1]
    with pytest.raises(KeyError):
        DAQTransmitter(
            journal=j, face_url="http://f", source="s", schema_ref={"scalars": "x"}, transport=ft
        )._schema_ref_for("grid")


def test_transmitter_reads_the_token_from_a_file(tmp_path):
    j = DAQJournal(tmp_path / "j")
    _cons(j).consume(_rec(0))
    tf = tmp_path / "key"
    tf.write_text("axk_fromfile\n")
    ft = _FakeTransport([200])
    tx = DAQTransmitter(
        journal=j, face_url="http://f", source="s", schema_ref="r", token_file=tf, transport=ft
    )
    tx.pump()
    assert ft.requests[0][2]["Authorization"] == "Bearer axk_fromfile"


# ---------------------------------------------------------------- dispatcher + producer


def test_cursor_isolation_a_stalled_subscriber_does_not_stall_others(tmp_path):
    j = DAQJournal(tmp_path)
    c = _cons(j)
    for i in range(3):
        c.consume(_rec(i))
    d = LiveDispatcher(j)
    seen_live: list[int] = []
    d.subscribe("display", lambda r: seen_live.append(r.envelope.seq))

    def stalled(r):
        raise RuntimeError("model busy")

    d.subscribe("model", stalled)
    ft = _FakeTransport([200])
    tx, _ = _tx(j, ft)
    res = d.pump()
    assert seen_live == [0, 1, 2] and res.failed["model"].startswith("RuntimeError")
    assert j.lag("sub.model") == 3 and j.lag("sub.display") == 0
    assert tx.pump().sent == 3  # the transmitter cursor is independent too


def test_journal_first_nothing_reaches_a_subscriber_that_was_not_journaled(tmp_path):
    """Spec §11: delivery before durable write is impossible by construction —
    the only path to a subscriber is a Journal cursor. Under block_producer
    with a full Journal, the Reader's samples reach nobody."""
    j = DAQJournal(tmp_path, max_bytes=1, policy=OverflowPolicy.BLOCK_PRODUCER)
    c = _cons(j)
    d = LiveDispatcher(j)
    seen: list[int] = []
    d.subscribe("display", lambda r: seen.append(r.envelope.seq))

    class Reader:
        def read(self):
            return [_rec(0), _rec(1)]

    p = Producer(reader=Reader(), consolidator=c, dispatcher=d, expected_interval_s=1.0)
    out = p.step()
    assert out["journaled"] == 0 and seen == [] and p.backpressure is True
    h = p.health()
    assert h["backpressure"] is True and h["journal_records"] == 0 and h["silent"] is True


def test_producer_step_end_to_end(tmp_path):
    j = DAQJournal(tmp_path)
    c = _cons(j)
    ft = _FakeTransport([200, 200])
    tx, _ = _tx(j, ft, batch_size=2)
    d = LiveDispatcher(j)
    seen: list[int] = []
    d.subscribe("display", lambda r: seen.append(r.envelope.seq))

    class Reader:
        def __init__(self):
            self.n = 0

        def read(self):
            self.n += 1
            return [_rec(self.n)] if self.n <= 3 else []

    p = Producer(
        reader=Reader(), consolidator=c, transmitter=tx, dispatcher=d, expected_interval_s=1.0
    )
    outs = [p.step() for _ in range(4)]
    assert [o["consumed"] for o in outs] == [1, 1, 1, 0]
    assert sum(o["sent"] for o in outs) == 3 and seen == [0, 1, 2]
    h = p.health()
    assert h["seq"] == 2 and h["transmitter"]["lag"] == 0 and h["subscribers"]["sub.display"] == 0
    assert h["silent"] is False and h["at_rest"] == "plaintext"


def test_producer_routes_multi_stream_samples_to_their_own_chains(tmp_path):
    j = DAQJournal(tmp_path)
    cons = {
        "instrument": _cons(j, stream="instrument"),
        "state": _cons(j, stream="state", source_class="estimated", model_ref="m@1"),
    }
    ft = _FakeTransport([200])
    tx = DAQTransmitter(
        journal=j, face_url="http://f", source="s", schema_ref={"*": "x/v1"}, transport=ft
    )

    class Reader:
        def read(self):
            return [
                ("instrument", _rec(0)),
                ("state", _rec(0, s=[1])),
                ("nope", _rec(0)),
                ("instrument", _rec(1)),
            ]

    p = Producer(reader=Reader(), consolidator=cons, transmitter=tx)
    out = p.step()
    assert out["consumed"] == 4 and out["journaled"] == 3 and p.unrouted == 1
    assert [r.envelope.seq for r in j.iter_all() if r.envelope.stream == "instrument"] == [0, 1]
    batches = {b["metadata"]["stream"]: b for b in ft.requests[0][1]["batches"]}
    assert set(batches) == {"instrument", "state"}
    assert batches["state"]["metadata"]["source_class"] == "estimated"
    h = p.health()
    assert set(h["streams"]) == {"instrument", "state"} and h["streams"]["instrument"]["seq"] == 1
    with pytest.raises(ValueError, match="one Journal"):
        Producer(
            reader=Reader(),
            consolidator={
                "a": _cons(j, stream="a"),
                "b": _cons(DAQJournal(tmp_path / "other"), stream="b"),
            },
        )


def test_reader_fault_is_health_not_a_crash(tmp_path):
    class Reader:
        def read(self):
            raise ConnectionError("upstream gone")

    p = Producer(reader=Reader(), consolidator=_cons(DAQJournal(tmp_path)))
    assert p.step()["consumed"] == 0
    assert p.reader_errors == 1 and "upstream gone" in p.health()["last_error"]


# ---------------------------------------------------------------- the face, for real


def test_transmitter_lands_rows_through_the_real_face_once_on_replay(tmp_path):
    """At-least-once meets the face's content_hash dedup: a replayed batch
    lands its rows once (spec §11 'idempotency')."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.http.server import create_app
    from axiom.rag.ingest_router import Disposition

    from ...bronze import FilesystemTabularBronzeSink, TabularBronzeWriter
    from ...ingest_sink import TabularIngestSink
    from ...ingest_sink.api import build_tabular_ingest_router

    writer = TabularBronzeWriter(
        rules=[],
        sink=FilesystemTabularBronzeSink(root=tmp_path / "bronze"),
        default_disposition=Disposition.ALLOW,
        default_tier="public",
    )
    app = create_app(title="t", version="0", description="")
    app.include_router(build_tabular_ingest_router(sink=TabularIngestSink(writer=writer)))
    client = TestClient(app)

    class ClientTransport:
        def post(self, url, body, headers):
            r = client.post(url.replace("http://face.example", ""), content=body, headers=headers)
            return r.status_code, r.text

    j = DAQJournal(tmp_path / "journal")
    c = _cons(j)
    for i in range(3):
        c.consume(_rec(i))
    tx, _ = _tx(j, ClientTransport())
    first = tx.pump()
    assert first.sent == 3 and first.responses[0]["rows_landed"] == 3
    # replay the same batch (simulate a lost ack): rewind the cursor
    j.commit("transmitter", 0)
    again = tx.pump()
    assert again.sent == 3 and again.responses[0]["rows_landed"] == 0
    assert again.responses[0]["rows_duplicate"] == 3


# ---------------------------------------------------------------- retention


def test_compact_releases_only_segments_every_cursor_has_passed(tmp_path):
    j = DAQJournal(tmp_path, segment_records=2)
    c = _cons(j)
    for i in range(7):  # segments: [0,1] [2,3] [4,5] [6]
        c.consume(_rec(i))
    assert j.compact() == 0  # no cursors → nothing is delivered history
    j.commit("transmitter", 7)
    j.commit("sub.display", 3)  # a subscriber still needs record 3 → segment [2,3] pinned
    assert j.compact() == 2 and j.head == 2
    j.commit("sub.display", 7)
    assert j.compact() == 4 and j.head == 6  # the live segment is always kept
    assert [r.envelope.seq for r in j.iter_all()] == [6]
    assert j.compacted == 6 and j.health_details()["compacted"] == 6
    # a restart still resumes the chain from the surviving tail
    assert _cons(DAQJournal(tmp_path, segment_records=2)).next_seq == 7
    # reads below head clamp to head
    assert [off for off, _ in j.read(0, 10)] == [6]


def test_producer_compacts_on_its_cadence(tmp_path):
    j = DAQJournal(tmp_path, segment_records=2)
    c = _cons(j)
    ft = _FakeTransport([200] * 50)
    tx, _ = _tx(j, ft, batch_size=100)

    class Reader:
        def read(self):
            return [_rec(0)]

    p = Producer(reader=Reader(), consolidator=c, transmitter=tx, compact_every=4)
    outs = [p.step() for _ in range(8)]
    assert "compacted" in outs[3] and "compacted" in outs[7]
    assert j.head > 0 and j.compacted > 0 and j.lag("transmitter") == 0
    assert (
        Producer(reader=Reader(), consolidator=c, compact_every=0).step().get("compacted") is None
    )
