# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""What a spooler can lose while it holds an outage, and whether it says so.

Some producer hosts have no outbound path for long, bounded stretches — an
isolated network, a link cut by an interlock, a vehicle out of coverage. Such a
host has to hold everything it produced and deliver it when the link returns,
which makes one failure mode central rather than theoretical: a bounded journal
on ``drop_oldest`` discards its oldest segments to make room, and

    read(offset, limit) -> offset = max(offset, self.head)

clamps a stale cursor up to the new head without comment. The consumer resumes
at the head, the batches it sends are internally contiguous, and the front of the
held period is simply gone. Afterwards ``lag`` reads as healthy and
``sent_total`` looks right.

The journal-wide ``dropped`` counter cannot answer this, and the distinction is
the whole point: a consumer that was caught up when the oldest segment went lost
nothing, and one that was behind lost the difference. Both show ``dropped > 0``.
"""

from __future__ import annotations

import json

import pytest

from ..consolidator import DAQConsolidator
from ..envelope import ConsolidatedRecord
from ..journal import DAQJournal, JournalFull, OverflowPolicy
from ..transmitter import DAQTransmitter

CURSOR = "transmitter"


def _rec(i: int) -> ConsolidatedRecord:
    """One telemetry sample."""
    return ConsolidatedRecord(
        schema_id="site-c/telemetry-v1",
        ts=f"2026-09-26T14:{i // 60 % 60:02d}:{i % 60:02d}.000Z",
        values={"Position": 100.0 + i * 0.01},
        tags={"unit.Position": "mm"},
    )


def _journal(tmp_path, *, max_bytes=None, policy=OverflowPolicy.DROP_OLDEST):
    return DAQJournal(
        tmp_path / "j", max_bytes=max_bytes, policy=policy, segment_records=50,
    )


def _spool(journal, count: int, *, tx=None, delivery_class="standard"):
    """Produce `count` samples through the REAL consolidator, so seq and the
    hash chain are what a live producer would have written."""
    cons = DAQConsolidator(
        journal=journal, producer_id="isolated-host", feed="telemetry",
        delivery_class=delivery_class,
    )
    for i in range(count):
        cons.consume(_rec(i))
        if tx is not None:
            tx.pump()  # trying and failing throughout, as it would


class _Dead:
    """A transport with no route out, which is the held condition."""

    def __init__(self):
        self.calls = 0

    def post(self, url, body, headers):
        self.calls += 1
        raise OSError("no route to host")


class _Alive:
    """Accepts everything and remembers the sequence numbers it was given.

    Reads the real wire shape — `{source, batches: [{rows: [...]}]}` — rather
    than one convenient to assert on, because the sequence numbers that actually
    crossed the wire are the evidence these tests turn on.
    """

    def __init__(self):
        self.seqs: list[int] = []

    def post(self, url, body, headers):
        rows = [
            row
            for batch in json.loads(body)["batches"]
            for row in batch["rows"]
        ]
        self.seqs.extend(row["seq"] for row in rows)
        return 200, json.dumps({"accepted": len(rows)})


def _transmitter(journal, transport):
    return DAQTransmitter(
        journal=journal, face_url="https://face.invalid", transport=transport,
        source="site-c", schema_ref="site-c/telemetry-v1",
        cursor_name=CURSOR, batch_size=100, jitter=lambda: 0.0,
    )


class TestTheLossIsCountedForTheConsumerThatSufferedIt:
    def test_a_cursor_left_behind_by_a_drop_reports_what_it_lost(self, tmp_path):
        journal = _journal(tmp_path, max_bytes=4096)
        journal.commit(CURSOR, 0)
        _spool(journal, 400)
        assert journal.dropped > 0, "the journal must actually have overflowed"
        assert journal.lost(CURSOR) == journal.head

    def test_a_caught_up_cursor_lost_NOTHING_though_records_were_dropped(self, tmp_path):
        """The test that makes the number specific rather than a restatement of
        `dropped`. Records discarded after a consumer delivered them are not a
        loss to that consumer, and a metric that cannot tell those apart would
        cry wolf on every healthy journal that ever rolled a segment."""
        journal = _journal(tmp_path, max_bytes=4096)
        _spool(journal, 400)
        journal.commit(CURSOR, journal.end)
        assert journal.dropped > 0
        assert journal.lost(CURSOR) == 0

    def test_an_unwritten_cursor_starts_at_the_head_and_lost_nothing(self, tmp_path):
        """A consumer that never ran has not lost anything; it simply has no
        claim on what came before it."""
        journal = _journal(tmp_path, max_bytes=4096)
        _spool(journal, 400)
        assert journal.lost("never-ran") == 0

    def test_it_is_reported_beside_the_lag_it_is_mistaken_for(self, tmp_path):
        journal = _journal(tmp_path, max_bytes=4096)
        journal.commit(CURSOR, 0)
        _spool(journal, 400)
        health = journal.health_details()
        assert health["cursor_lost"][CURSOR] == journal.head
        assert health["cursor_lag"][CURSOR] > 0


class TestHoldingAWholeOutage:
    """No outbound path for the duration, then reconnect."""

    def test_a_journal_sized_for_the_run_delivers_all_of_it(self, tmp_path):
        journal = _journal(tmp_path)  # unbounded
        tx = _transmitter(journal, _Dead())
        _spool(journal, 600, tx=tx)
        assert journal.lost(CURSOR) == 0

        alive = _Alive()
        tx.transport = alive
        tx._not_before = 0.0
        for _ in range(20):
            if not tx.pump().sent:
                break
        assert alive.seqs == list(range(600)), "complete and in order"
        assert tx.lost_total == 0

    def test_a_journal_too_small_for_the_run_loses_the_front_of_it(self, tmp_path):
        """And the delivered feed is contiguous, which is what makes this
        dangerous: nothing in what arrived says anything is missing."""
        journal = _journal(tmp_path, max_bytes=4096)
        journal.commit(CURSOR, 0)
        tx = _transmitter(journal, _Dead())
        _spool(journal, 600, tx=tx)

        alive = _Alive()
        tx.transport = alive
        tx._not_before = 0.0
        for _ in range(20):
            if not tx.pump().sent:
                break

        assert alive.seqs, "something was delivered"
        assert alive.seqs[0] > 0, "the front of the run never arrived"
        # Internally contiguous: the hole is at the START, invisible in the data.
        assert alive.seqs == list(range(alive.seqs[0], alive.seqs[-1] + 1))

    def test_the_transmitter_latches_the_loss_so_it_survives_catching_up(self, tmp_path):
        """The reason this is latched rather than derived on demand. The
        instantaneous value is zero the moment the consumer catches up, so a
        spooler asked afterwards what it never sent could not answer."""
        journal = _journal(tmp_path, max_bytes=4096)
        journal.commit(CURSOR, 0)
        tx = _transmitter(journal, _Dead())
        _spool(journal, 600, tx=tx)

        tx.transport = _Alive()
        tx._not_before = 0.0
        for _ in range(20):
            if not tx.pump().sent:
                break

        assert journal.lost(CURSOR) == 0, "caught up, so the live value is gone"
        assert tx.lost_total > 0, "but the spooler can still say what it lost"
        assert tx.health_details()["lost_total"] == tx.lost_total

    def test_the_pump_that_skipped_says_so_in_its_own_result(self, tmp_path):
        journal = _journal(tmp_path, max_bytes=4096)
        journal.commit(CURSOR, 0)
        _spool(journal, 400)
        tx = _transmitter(journal, _Alive())
        assert tx.pump().lost == journal.head

    def test_a_clean_pump_reports_no_loss(self, tmp_path):
        journal = _journal(tmp_path)
        _spool(journal, 10)
        journal.commit(CURSOR, 0)
        tx = _transmitter(journal, _Alive())
        result = tx.pump()
        assert result.sent == 10
        assert result.lost == 0
        assert tx.lost_total == 0


class TestOffsetsStayMonotonic:
    """The defect the loss counter uncovered, and the more serious of the two.

    ``drop_oldest`` pops segments to make room. When the byte cap is reached
    inside a single segment — which is the normal case for any journal whose cap
    is smaller than ``segment_records`` records — it pops the ONLY segment, and
    ``_segments`` goes empty. ``end`` then answered 0, so the next segment was
    created at base 0 and **the global offset space restarted**.

    Every cursor is an offset into that space. A restart makes a cursor that was
    legitimately at 500 point past the end of a journal that has just gone back
    to 8, so it reads nothing and the producer stalls in silence; and it makes
    two different records share one offset.
    """

    def test_the_end_never_goes_backwards_when_the_last_segment_is_dropped(self, tmp_path):
        journal = _journal(tmp_path, max_bytes=4096)
        _spool(journal, 600)
        assert journal.dropped > 0, "the single-segment drop path must have run"
        assert journal.end >= 600, f"offsets restarted: end={journal.end}"

    def test_the_head_of_a_drained_journal_is_not_zero(self, tmp_path):
        """A journal that has dropped 600 records has not returned to the
        beginning, and a cursor comparing itself against a head of 0 would
        conclude it was up to date."""
        journal = _journal(tmp_path, max_bytes=4096)
        _spool(journal, 600)
        assert journal.head > 0

    def test_a_reopened_journal_does_not_reuse_spent_offsets(self, tmp_path):
        """The floor is persisted, so a restart after a drain cannot hand the
        next record an offset an earlier record already had."""
        first = _journal(tmp_path, max_bytes=4096)
        _spool(first, 600)
        end_before = first.end

        reopened = _journal(tmp_path, max_bytes=4096)
        assert reopened.end >= end_before
        assert reopened.head == first.head

    def test_a_cursor_is_never_left_pointing_past_the_end(self, tmp_path):
        """What the offset reset actually did to a consumer: strand it beyond a
        journal that had shrunk behind it, where read() returns nothing forever
        and nothing reports a problem."""
        journal = _journal(tmp_path, max_bytes=4096)
        _spool(journal, 600)
        journal.commit(CURSOR, 500)
        assert journal.cursor(CURSOR) <= journal.end
        assert journal.read(journal.cursor(CURSOR), 100), "a live cursor must read"

    def test_the_floor_is_reported(self, tmp_path):
        """It is a lower bound on the offset space, so it advances past zero once
        anything is dropped and never exceeds the end. It is not the end: records
        that survive the drop sit between the two."""
        journal = _journal(tmp_path, max_bytes=4096)
        _spool(journal, 600)
        floor = journal.health_details()["offset_floor"]
        assert 0 < floor <= journal.end
        assert floor == journal.head


class TestTheWayToNotLoseIt:
    """`drop_oldest` is the wrong policy for a spooler that must be complete."""

    def test_block_producer_refuses_the_write_instead_of_losing_it(self, tmp_path):
        journal = _journal(
            tmp_path, max_bytes=4096, policy=OverflowPolicy.BLOCK_PRODUCER)
        journal.commit(CURSOR, 0)
        with pytest.raises(JournalFull):
            _spool(journal, 600)
        assert journal.lost(CURSOR) == 0, "nothing was silently discarded"

    def test_trip_on_gap_refuses_storage_and_counts_the_trip(self, tmp_path):
        journal = _journal(tmp_path, max_bytes=4096, policy=OverflowPolicy.TRIP_ON_GAP)
        journal.commit(CURSOR, 0)
        # Only legal for a credited feed, which is the platform making the
        # same point this test does: the policy that refuses to lose anything is
        # reserved for the feeds that must not lose anything.
        _spool(journal, 600, delivery_class="credited")
        assert journal.trips > 0, "storage was refused rather than making room"
        assert journal.lost(CURSOR) == 0
