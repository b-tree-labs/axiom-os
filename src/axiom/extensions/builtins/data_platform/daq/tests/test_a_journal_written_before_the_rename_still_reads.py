"""A journal written before the ``stream`` → ``feed`` rename still reads and verifies.

The rename (0.61.1) changed the envelope key on disk *and* the key the content
hash covers. A node's journal outlives the code that wrote it, so on upgrade
the producer replayed records that said ``stream`` into a reader that only knew
``feed`` and crashed on start. These tests pin the two halves of the fix: the
legacy record loads, and its chain still verifies against the hash it was
written with, without making mutation any easier to hide.
"""

from __future__ import annotations

import hashlib

import pytest

from axiom.extensions.builtins.data_platform.daq.envelope import (
    ConsolidatedRecord,
    JournaledRecord,
    canonical_json,
    verify_chain,
)


def _legacy_hash(envelope: dict, record: ConsolidatedRecord) -> str:
    """The content hash exactly as a pre-0.61.1 producer computed it."""
    payload = {
        "producer_id": envelope["producer_id"],
        "stream": envelope["stream"],
        "seq": envelope["seq"],
        "prev_hash": envelope["prev_hash"],
        "delivery_class": envelope["delivery_class"],
        "payload_kind": envelope["payload_kind"],
        "source_class": envelope["source_class"],
        "model_ref": envelope["model_ref"],
        "record": record.to_dict(),
    }
    return "sha256:" + hashlib.sha256(canonical_json(payload)).hexdigest()


def _legacy_chain(n: int = 3) -> list[dict]:
    """``n`` journal lines in the pre-rename shape, correctly chained."""
    lines: list[dict] = []
    prev: str | None = None
    for seq in range(n):
        record = ConsolidatedRecord(
            schema_id="demo/v1", ts=f"2026-09-01T00:00:0{seq}Z", values={"x": seq}
        )
        envelope = {
            "producer_id": "p1",
            "stream": "demo",
            "seq": seq,
            "prev_hash": prev,
            "delivery_class": "standard",
            "payload_kind": "records",
            "source_class": "measured",
            "model_ref": None,
            "manifest_sig": None,
            "stamp_alg": None,
        }
        envelope["content_hash"] = _legacy_hash(envelope, record)
        lines.append({"envelope": envelope, "record": record.to_dict()})
        prev = envelope["content_hash"]
    return lines


def test_a_legacy_record_loads_under_the_new_name():
    rec = JournaledRecord.from_dict(_legacy_chain(1)[0])
    assert rec.envelope.feed == "demo"


def test_a_legacy_chain_verifies_clean():
    records = [JournaledRecord.from_dict(d) for d in _legacy_chain()]
    assert verify_chain(records) == []


def test_a_legacy_record_reserialises_under_the_new_name_and_still_verifies():
    records = [JournaledRecord.from_dict(d) for d in _legacy_chain()]
    round_tripped = [JournaledRecord.from_dict(r.to_dict()) for r in records]
    assert "stream" not in round_tripped[0].to_dict()["envelope"]
    assert verify_chain(round_tripped) == []


def test_a_mutated_legacy_record_is_still_caught():
    lines = _legacy_chain()
    lines[1]["record"]["values"] = {"x": 999}
    faults = verify_chain([JournaledRecord.from_dict(d) for d in lines])
    assert [(f.seq, f.kind) for f in faults] == [(1, "mutated")]


def test_a_record_naming_two_different_feeds_is_refused():
    line = _legacy_chain(1)[0]
    line["envelope"]["feed"] = "other"
    with pytest.raises(ValueError, match="two different feeds"):
        JournaledRecord.from_dict(line)


def test_a_record_naming_the_same_feed_twice_loads():
    line = _legacy_chain(1)[0]
    line["envelope"]["feed"] = "demo"
    assert JournaledRecord.from_dict(line).envelope.feed == "demo"
