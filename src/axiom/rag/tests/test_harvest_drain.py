# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Queued intent becomes retrievable text, or says why it did not.

The drain is where everything expensive lives — embedding and the store write —
so the properties that matter are about failure: what happens to the queue when
one record fails, and whether anything can vanish silently.
"""

from __future__ import annotations

from axiom.rag import harvest_drain
from axiom.rag import harvest_queue as q


class _Store:
    """Records what the drain asked of a RAGStore."""

    def __init__(self, fail_on: str | None = None):
        self.upserts: list[dict] = []
        self.deletes: list[tuple] = []
        self._fail_on = fail_on

    def upsert_chunks(self, chunks, embeddings=None, **kw):
        if self._fail_on and chunks[0].source_path.endswith(self._fail_on):
            raise RuntimeError("store is unhappy")
        self.upserts.append({"chunks": chunks, "embeddings": embeddings, **kw})

    def delete_document(self, source_path, corpus=None):
        self.deletes.append((source_path, corpus))


def _q(path, eid="1", op="upsert", text="Sample record.\nlabel: core-7"):
    q.enqueue(path, q.HarvestIntent(
        entity_type="sample", entity_id=eid, op=op,
        corpus="rag-org", owner="site-a", title=f"Sample {eid}",
        text="" if op == "delete" else text,
    ))


def test_a_card_becomes_one_chunk_in_the_right_corpus(tmp_path):
    """One record's facts belong in one result, not scattered across several."""
    path = tmp_path / "h.jsonl"
    _q(path)
    store = _Store()

    report = harvest_drain.drain(path, store)

    assert report.upserted == 1 and report.ok
    call = store.upserts[0]
    assert len(call["chunks"]) == 1
    assert call["corpus"] == "rag-org" and call["owner"] == "site-a"
    assert call["data_source"] == "harvest"
    assert call["chunks"][0].source_path == "entity://sample/1"


def test_a_delete_removes_the_document(tmp_path):
    path = tmp_path / "h.jsonl"
    _q(path, eid="2", op="delete")
    store = _Store()

    report = harvest_drain.drain(path, store)

    assert report.deleted == 1
    assert store.deletes == [("entity://sample/2", "rag-org")]


def test_without_an_embedder_the_card_is_still_indexed(tmp_path):
    """Degraded retrieval beats writing nothing at all."""
    path = tmp_path / "h.jsonl"
    _q(path)
    store = _Store()
    harvest_drain.drain(path, store)
    assert store.upserts[0]["embeddings"] is None


def test_an_embedder_is_used_when_given(tmp_path):
    path = tmp_path / "h.jsonl"
    _q(path)
    store = _Store()
    harvest_drain.drain(path, store, embedder=lambda texts: [[0.1, 0.2]] * len(texts))
    assert store.upserts[0]["embeddings"] == [[0.1, 0.2]]


def test_a_successful_drain_clears_the_queue(tmp_path):
    path = tmp_path / "h.jsonl"
    _q(path)
    harvest_drain.drain(path, _Store())
    assert q.read_pending(path) == []


def test_a_failed_record_keeps_the_queue_for_a_retry(tmp_path):
    """Clearing after partial success drops the failures silently — and a
    record that never becomes retrievable is invisible."""
    path = tmp_path / "h.jsonl"
    _q(path, eid="1")
    _q(path, eid="2")
    store = _Store(fail_on="/2")

    report = harvest_drain.drain(path, store)

    assert report.upserted == 1
    assert report.failed == ["sample/2"]
    assert not report.ok
    assert q.read_pending(path), "the queue was cleared despite a failure"


def test_one_failure_does_not_stop_the_others(tmp_path):
    path = tmp_path / "h.jsonl"
    for i in ("1", "2", "3"):
        _q(path, eid=i)
    store = _Store(fail_on="/2")

    report = harvest_drain.drain(path, store)
    assert report.upserted == 2


def test_an_upsert_with_no_card_is_refused_not_written(tmp_path):
    """Writing it would replace a real record with an empty one."""
    path = tmp_path / "h.jsonl"
    q.enqueue(path, q.HarvestIntent(
        entity_type="sample", entity_id="9", op="upsert",
        corpus="rag-org", owner="site-a", title="Sample 9", text="",
    ))
    store = _Store()
    report = harvest_drain.drain(path, store)

    assert store.upserts == []
    assert "no card" in report.failed[0]


def test_an_empty_queue_is_a_no_op(tmp_path):
    report = harvest_drain.drain(tmp_path / "nothing.jsonl", _Store())
    assert report.upserted == 0 and report.deleted == 0 and report.ok
