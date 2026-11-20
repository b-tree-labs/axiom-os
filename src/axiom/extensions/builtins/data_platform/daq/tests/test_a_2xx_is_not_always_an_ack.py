# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The receiver's acknowledgement is not a boolean, and we were reading it as one.

The delivery link is cumulative-ACK with retransmission: the journal holds a
sequence, the cursor is the acknowledged offset, a 2xx advances it, and
anything not acknowledged is sent again. That is TCP's shape and it is sound.

What was missing is the part of TCP's ACK that carries a number. The face
answers with `TabularIngestResult` — `accepted`, `landed`, `excluded`,
`errored`, `rows_in`, `rows_landed`, `rows_duplicate` — and the transmitter
parsed that body, stored it in `result.responses`, and acted on none of it. On
any 2xx it committed the cursor and reported `sent = len(items)`.

The loss is concrete. A batch whose write raises is caught by the face so that
"one bad batch must not sink the push", counted in `errored`, and its rows are
never even added to `rows_in`. The request still returns 200. So the sender
advanced past rows the receiver told it, in the same response, that it had
dropped.

`excluded` is NOT that. A batch gated to EXCLUDE is a policy decision the
receiver made on purpose, and treating it as a failure would have a site
retrying forever against a rule that is working.
"""

from __future__ import annotations

import json

import pytest

from ..consolidator import DAQConsolidator
from ..envelope import ConsolidatedRecord
from ..journal import DAQJournal
from ..transmitter import DAQTransmitter


def _rec(i: int) -> ConsolidatedRecord:
    return ConsolidatedRecord(
        schema_id="site-b/epics-v1",
        ts=f"2026-09-25T00:00:{i:02d}Z",
        values={"STC1": 1.0 + i},
    )


class _Face:
    """A face that answers 200 with whatever body the test names."""

    def __init__(self, body: dict, status: int = 200):
        self.body = body
        self.status = status
        self.posts = 0

    def post(self, url, body, headers):
        self.posts += 1
        return self.status, json.dumps(self.body)


@pytest.fixture
def journal(tmp_path):
    """Three records, written through the consolidator so the chain is real."""
    j = DAQJournal(tmp_path / "journal")
    cons = DAQConsolidator(journal=j, producer_id="site-b", feed="loop")
    for i in range(3):
        cons.consume(_rec(i))
    return j


def _tx(journal, face, **kw):
    return DAQTransmitter(
        face_url="http://face.invalid",
        source="site-b",
        schema_ref="site-b/epics-v1",
        token="t",
        transport=face,
        journal=journal,
        **kw,
    )


def _ok(**over):
    body = {
        "source": "site-b", "accepted": 1, "landed": 1, "excluded": 0,
        "errored": 0, "rows_in": 3, "rows_landed": 3, "rows_duplicate": 0,
    }
    body.update(over)
    return body


class TestAFullAckStillWorks:
    def test_everything_landed_advances_the_cursor(self, journal):
        tx = _tx(journal, _Face(_ok()))
        result = tx.pump()
        assert result.sent == 3
        assert journal.cursor(tx.cursor_name) == 3

    def test_duplicates_are_an_ack(self, journal):
        """Re-sending is how at-least-once works, and the receiver's dedup is
        what makes it safe. A duplicate is a success, not a loss."""
        tx = _tx(journal, _Face(_ok(rows_landed=0, rows_duplicate=3, landed=0)))
        result = tx.pump()
        assert result.sent == 3
        assert journal.cursor(tx.cursor_name) == 3

    def test_a_deliberate_exclusion_is_an_ack(self, journal):
        """A batch gated to EXCLUDE is a rule working. Retrying it would have
        a site pushing forever against a policy that is doing its job."""
        tx = _tx(journal, _Face(_ok(landed=0, excluded=1, rows_landed=0)))
        result = tx.pump()
        assert result.sent == 3
        assert journal.cursor(tx.cursor_name) == 3


class TestAPartialAckIsNotAFullOne:
    def test_the_cursor_does_not_advance_past_dropped_rows(self, journal):
        """The whole point. The face said it dropped a batch; advancing past
        it loses those rows with no dead-letter and no counter."""
        tx = _tx(journal, _Face(_ok(errored=1, landed=0, rows_in=0, rows_landed=0)))
        tx.pump()
        assert journal.cursor(tx.cursor_name) == 0

    def test_it_is_not_reported_as_sent(self, journal):
        tx = _tx(journal, _Face(_ok(errored=1, landed=0, rows_in=0, rows_landed=0)))
        result = tx.pump()
        assert result.sent == 0
        assert result.deferred == 3

    def test_it_says_what_the_face_said(self, journal):
        """An operator needs to know this was a receiver-side write failure
        and not a network fault, because the two have different owners."""
        tx = _tx(journal, _Face(_ok(errored=1, landed=0, rows_in=0, rows_landed=0)))
        result = tx.pump()
        assert "errored" in (result.detail or "").lower()

    def test_it_backs_off_like_any_other_deferral(self, journal):
        tx = _tx(journal, _Face(_ok(errored=1, landed=0, rows_in=0, rows_landed=0)))
        result = tx.pump()
        assert result.retry_after_s and result.retry_after_s > 0

    def test_the_rows_are_retried(self, journal):
        """Retransmission is the whole reason the cursor did not move."""
        face = _Face(_ok(errored=1, landed=0, rows_in=0, rows_landed=0))
        tx = _tx(journal, face, backoff=(0,))
        tx.pump()
        tx.pump()
        assert face.posts == 2


class TestItCannotBlockForever:
    """A permanently bad batch must not pin the cursor. TCP has no answer for
    this because TCP has no notion of a payload the receiver will never take;
    a durable link does, and the answer is a bounded number of attempts and
    then a dead-letter."""

    def test_a_batch_that_keeps_erroring_is_eventually_dead_lettered(self, journal):
        face = _Face(_ok(errored=1, landed=0, rows_in=0, rows_landed=0))
        tx = _tx(journal, face, backoff=(0,), max_partial_retries=2)
        for _ in range(4):
            tx.pump()
        assert journal.cursor(tx.cursor_name) == 3

    def test_the_dead_letter_records_why(self, journal, tmp_path):
        face = _Face(_ok(errored=1, landed=0, rows_in=0, rows_landed=0))
        tx = _tx(journal, face, backoff=(0,), max_partial_retries=1)
        for _ in range(3):
            tx.pump()
        assert tx.refused_total == 3

    def test_a_recovery_before_the_limit_clears_the_count(self, journal):
        """A transient receiver fault must not consume the budget that exists
        for a permanent one."""
        face = _Face(_ok(errored=1, landed=0, rows_in=0, rows_landed=0))
        tx = _tx(journal, face, backoff=(0,), max_partial_retries=2)
        tx.pump()
        face.body = _ok()
        tx.pump()
        assert journal.cursor(tx.cursor_name) == 3
        assert tx.refused_total == 0


class TestAnUnreadableBodyIsNotAnAck:
    def test_a_2xx_with_no_body_is_still_an_ack(self, journal):
        """Not every face answers with counts, and a peer that returns a bare
        200 has not told us anything is wrong. Refusing to advance on silence
        would stall every such link."""
        tx = _tx(journal, _Face({}))
        assert tx.pump().sent == 3

    def test_a_2xx_with_unparseable_text_is_still_an_ack(self, journal):
        class _Garbage:
            posts = 0

            def post(self, url, body, headers):
                return 200, "<html>ok</html>"

        tx = _tx(journal, _Garbage())
        assert tx.pump().sent == 3
