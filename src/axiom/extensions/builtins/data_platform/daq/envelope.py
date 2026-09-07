# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The signal envelope (spec-signal-ingest-and-producer §3) and the hash chain.

A :class:`ConsolidatedRecord` is what a Reader's sample becomes after
consolidation (``schema_id, ts, values, tags, quality, sensitivity``). The
Consolidator stamps a :class:`SignalEnvelope` on it — identity, gapless
``seq``, ``prev_hash``/``content_hash`` chain, classification — and the pair is
what the Journal stores and every consumer reads.

``site`` is deliberately absent: it is derived from the authenticated principal
at the ingest face, never carried in the payload.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

DELIVERY_CLASSES: tuple[str, ...] = ("standard", "credited")
PAYLOAD_KINDS: tuple[str, ...] = ("records", "artifact")
SOURCE_CLASSES: tuple[str, ...] = ("measured", "predicted", "estimated")
QUALITIES: tuple[str, ...] = ("good", "uncertain", "bad")
SENSITIVITY_CLASSES: tuple[str, ...] = ("open", "internal", "ec-controlled", "itar")
CONTROLLED_SENSITIVITIES: frozenset[str] = frozenset({"ec-controlled", "itar"})

HASH_PREFIX = "sha256:"


def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON: sorted keys, no whitespace, non-JSON types via str."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _digest(payload: bytes) -> str:
    return HASH_PREFIX + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class ConsolidatedRecord:
    """One consolidated sample (the subsystem contract's record shape)."""

    schema_id: str
    ts: str  # ISO-8601 UTC, the Reader's wall clock (spec §12 open question)
    values: Mapping[str, Any]
    tags: Mapping[str, str] = field(default_factory=dict)
    quality: str = "good"
    sensitivity: str = "open"

    def __post_init__(self) -> None:
        if self.quality not in QUALITIES:
            raise ValueError(f"quality must be one of {QUALITIES}: {self.quality!r}")
        if self.sensitivity not in SENSITIVITY_CLASSES:
            raise ValueError(
                f"sensitivity must be one of {SENSITIVITY_CLASSES}: {self.sensitivity!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["values"] = dict(self.values)
        d["tags"] = dict(self.tags)
        return d

    def record_hash(self) -> str:
        """Hash of what the record *says* — values, tags, quality, sensitivity,
        schema — excluding ``ts``. This is the stuck-value probe (spec §11):
        a source re-emitting the same reading under a fresh timestamp is
        exactly the case it must catch, so the timestamp cannot be in it."""
        d = self.to_dict()
        d.pop("ts", None)
        return _digest(canonical_json(d))


@dataclass(frozen=True)
class SignalEnvelope:
    """Identity, ordering + chain, classification, reserved provenance (§3)."""

    producer_id: str
    stream: str
    seq: int
    prev_hash: str | None
    content_hash: str
    delivery_class: str = "standard"
    payload_kind: str = "records"
    source_class: str = "measured"
    model_ref: str | None = None
    manifest_sig: str | None = None
    stamp_alg: str | None = None

    def __post_init__(self) -> None:
        if self.delivery_class not in DELIVERY_CLASSES:
            raise ValueError(f"delivery_class must be one of {DELIVERY_CLASSES}")
        if self.payload_kind not in PAYLOAD_KINDS:
            raise ValueError(f"payload_kind must be one of {PAYLOAD_KINDS}")
        if self.source_class not in SOURCE_CLASSES:
            raise ValueError(f"source_class must be one of {SOURCE_CLASSES}")
        if self.source_class != "measured" and not self.model_ref:
            raise ValueError("model_ref is required when source_class is not 'measured'")
        if self.seq < 0:
            raise ValueError("seq must be >= 0")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def hash_input(
    *,
    producer_id: str,
    stream: str,
    seq: int,
    prev_hash: str | None,
    delivery_class: str,
    payload_kind: str,
    source_class: str,
    model_ref: str | None,
    record: ConsolidatedRecord,
) -> dict[str, Any]:
    """Everything ``content_hash`` covers: identity + ordering + classification +
    the record. Any scale/unit factor lives in the record, so it is covered."""
    return {
        "producer_id": producer_id,
        "stream": stream,
        "seq": seq,
        "prev_hash": prev_hash,
        "delivery_class": delivery_class,
        "payload_kind": payload_kind,
        "source_class": source_class,
        "model_ref": model_ref,
        "record": record.to_dict(),
    }


def compute_content_hash(envelope_fields: Mapping[str, Any], record: ConsolidatedRecord) -> str:
    return _digest(canonical_json(hash_input(record=record, **envelope_fields)))


@dataclass(frozen=True)
class JournaledRecord:
    """What the Journal stores and every cursor reads: envelope + record."""

    envelope: SignalEnvelope
    record: ConsolidatedRecord

    def to_dict(self) -> dict[str, Any]:
        return {"envelope": self.envelope.to_dict(), "record": self.record.to_dict()}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> JournaledRecord:
        return cls(
            envelope=SignalEnvelope(**dict(d["envelope"])),
            record=ConsolidatedRecord(**dict(d["record"])),
        )

    def to_row(self) -> dict[str, Any]:
        """The flat row the ingest face lands: envelope fields + record fields."""
        row = self.envelope.to_dict()
        row.update(self.record.to_dict())
        return row

    def expected_hash(self) -> str:
        e = self.envelope
        return compute_content_hash(
            {
                "producer_id": e.producer_id,
                "stream": e.stream,
                "seq": e.seq,
                "prev_hash": e.prev_hash,
                "delivery_class": e.delivery_class,
                "payload_kind": e.payload_kind,
                "source_class": e.source_class,
                "model_ref": e.model_ref,
            },
            self.record,
        )


@dataclass(frozen=True)
class ChainFault:
    seq: int
    kind: str  # 'gap' | 'mutated' | 'broken_link'
    detail: str


def verify_chain(records: Iterable[JournaledRecord]) -> list[ChainFault]:
    """Walk one stream's records in order and report every fault: a missing
    ``seq`` (gap), a record whose content no longer matches its own hash
    (mutated), and a ``prev_hash`` that does not point at its predecessor
    (broken link). No cryptography — the chain alone gives gap detection and
    tamper evidence (§10)."""
    faults: list[ChainFault] = []
    prev: JournaledRecord | None = None
    for rec in records:
        e = rec.envelope
        if prev is not None and e.seq != prev.envelope.seq + 1:
            faults.append(ChainFault(e.seq, "gap", f"seq {prev.envelope.seq} → {e.seq}"))
        if rec.expected_hash() != e.content_hash:
            faults.append(ChainFault(e.seq, "mutated", "content_hash does not match content"))
        if prev is not None and e.seq == prev.envelope.seq + 1:
            if e.prev_hash != prev.envelope.content_hash:
                faults.append(ChainFault(e.seq, "broken_link", "prev_hash != predecessor hash"))
        prev = rec
    return faults


__all__ = [
    "CONTROLLED_SENSITIVITIES",
    "DELIVERY_CLASSES",
    "PAYLOAD_KINDS",
    "QUALITIES",
    "SENSITIVITY_CLASSES",
    "SOURCE_CLASSES",
    "ChainFault",
    "ConsolidatedRecord",
    "JournaledRecord",
    "SignalEnvelope",
    "canonical_json",
    "compute_content_hash",
    "verify_chain",
]
