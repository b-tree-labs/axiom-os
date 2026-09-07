# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The Consolidator — sensitivity filter, envelope stamp, Journal first.

One Consolidator per ``(producer_id, stream)``. It resumes ``seq`` and
``prev_hash`` from the Journal's tail so a restart continues the chain with no
gap and no duplicate ``seq``; it applies the site's sensitivity filter
*before* anything is stored; and the **only** thing it does with a stamped
record is append it to the Journal — fan-out happens from Journal cursors, so
delivery-before-durability is impossible by construction (spec §11).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from .envelope import (
    ConsolidatedRecord,
    JournaledRecord,
    SignalEnvelope,
    compute_content_hash,
)
from .journal import DAQJournal, validate_overflow_policy

SensitivityFilter = Callable[[ConsolidatedRecord], ConsolidatedRecord | None]
"""Return the record to keep (possibly redacted) or ``None`` to drop it."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class DAQConsolidator:
    def __init__(
        self,
        *,
        producer_id: str,
        stream: str,
        journal: DAQJournal,
        delivery_class: str = "standard",
        payload_kind: str = "records",
        source_class: str = "measured",
        model_ref: str | None = None,
        sensitivity: str = "open",
        sensitivity_filter: SensitivityFilter | None = None,
        stuck_limit: int = 0,
    ) -> None:
        """``sensitivity`` is the stream's declared class — the Journal's overflow
        policy is validated against it and ``delivery_class`` up front.
        ``stuck_limit`` > 0 flags a stream whose record content has not changed
        for that many consecutive records (spec §11 'stuck value')."""
        if not producer_id or not stream:
            raise ValueError("producer_id and stream are required")
        validate_overflow_policy(
            journal.policy, sensitivity=sensitivity, delivery_class=delivery_class
        )
        # validate the classification once, via the envelope's own rules
        SignalEnvelope(
            producer_id=producer_id,
            stream=stream,
            seq=0,
            prev_hash=None,
            content_hash="sha256:0",
            delivery_class=delivery_class,
            payload_kind=payload_kind,
            source_class=source_class,
            model_ref=model_ref,
        )
        self.producer_id = producer_id
        self.stream = stream
        self.journal = journal
        self.delivery_class = delivery_class
        self.payload_kind = payload_kind
        self.source_class = source_class
        self.model_ref = model_ref
        self.sensitivity = sensitivity
        self.sensitivity_filter = sensitivity_filter
        self.stuck_limit = stuck_limit
        self.filtered = 0
        self.stuck_run = 0
        self.stuck_events = 0
        self.last_record_at: str | None = None
        self._last_record_hash: str | None = None
        tail = journal.tail(producer_id=producer_id, stream=stream)
        if tail is None:
            self._next_seq = 0
            self._prev_hash: str | None = None
        else:
            self._next_seq = tail.envelope.seq + 1
            self._prev_hash = tail.envelope.content_hash

    @property
    def next_seq(self) -> int:
        return self._next_seq

    def consume(self, record: ConsolidatedRecord) -> JournaledRecord | None:
        """Filter → stamp → Journal. Returns the journaled record, or ``None``
        when the filter dropped it or ``trip_on_gap`` refused storage (the seq
        is consumed either way only in the latter case)."""
        if self.sensitivity_filter is not None:
            kept = self.sensitivity_filter(record)
            if kept is None:
                self.filtered += 1
                return None
            record = kept
        fields = {
            "producer_id": self.producer_id,
            "stream": self.stream,
            "seq": self._next_seq,
            "prev_hash": self._prev_hash,
            "delivery_class": self.delivery_class,
            "payload_kind": self.payload_kind,
            "source_class": self.source_class,
            "model_ref": self.model_ref,
        }
        env = SignalEnvelope(content_hash=compute_content_hash(fields, record), **fields)
        jr = JournaledRecord(envelope=env, record=record)
        offset = self.journal.append(jr)  # durability first — the only side effect
        # the seq is consumed whether or not storage happened: a refused record
        # under trip_on_gap leaves the gap consumers trip on
        self._next_seq += 1
        self._prev_hash = env.content_hash
        self.last_record_at = _now_iso()
        rh = record.record_hash()
        if rh == self._last_record_hash:
            self.stuck_run += 1
            if self.stuck_limit and self.stuck_run >= self.stuck_limit:
                self.stuck_events += 1
        else:
            self.stuck_run = 0
        self._last_record_hash = rh
        return jr if offset is not None else None

    def health_details(self) -> dict:
        return {
            "producer_id": self.producer_id,
            "stream": self.stream,
            "seq": self._next_seq - 1,
            "last_record_at": self.last_record_at,
            "filtered": self.filtered,
            "stuck_run": self.stuck_run,
            "stuck_events": self.stuck_events,
            "delivery_class": self.delivery_class,
            "source_class": self.source_class,
        }


__all__ = ["DAQConsolidator", "SensitivityFilter"]
