# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The transmitter honours rate limits, and does it through the one parser.

Two things, and the first is a correction to my own work from an hour earlier.

**There was already a parser.** `axiom.infra.ratelimit.parse_headers` reads
`X-RateLimit-Limit`, `-Remaining`, `-Reset` and `Retry-After`, matches header
names case-insensitively, and handles the RFC 7231 §7.1.3 HTTP-date form of
`Retry-After`. It was written after a connector ignored every one of those and
"the first 429 took the whole run down rather than self-pacing through it" —
the same failure, one lane over.

I wrote a second, worse parser in the transmitter: seconds only, no HTTP-date,
its own case-insensitive lookup. Two implementations of "when does the server
want me back" is how they stop agreeing, and the one a partner's node runs was
the one that understood less.

**Reacting to 429 is not pacing.** A limit is published on every response, not
only on the one that refuses. `RateLimitWindow.should_throttle` says when the
budget is nearly spent, and using it means a site slows down before it is
refused rather than after — which is the difference between a link that paces
and a link that gets rejected and retries.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

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


@pytest.fixture
def journal(tmp_path):
    j = DAQJournal(tmp_path / "journal")
    cons = DAQConsolidator(journal=j, producer_id="site-b", feed="loop")
    for i in range(6):
        cons.consume(_rec(i))
    return j


class _Face:
    def __init__(self, status=200, body=None, headers=None):
        self.status = status
        self.body = json.dumps(body if body is not None else {"errored": 0})
        self.headers = headers or {}
        self.posts = 0

    def post(self, url, body, headers):
        self.posts += 1
        return self.status, self.body, self.headers


def _tx(journal, face, **kw):
    kw.setdefault("jitter", None)
    return DAQTransmitter(
        face_url="http://face.invalid",
        source="site-b",
        schema_ref="site-b/epics-v1",
        token="t",
        transport=face,
        journal=journal,
        **kw,
    )


class TestItUsesTheOneParser:
    def test_the_transmitter_does_not_carry_its_own(self):
        """A second parser for the same headers is how the two stop agreeing,
        and a partner's node would run whichever understood less."""
        import inspect

        from .. import transmitter

        source = inspect.getsource(transmitter)
        assert "infra.ratelimit" in source or "from axiom.infra import ratelimit" in source
        assert "def _retry_after(" not in source

    def test_the_http_date_form_is_understood(self, journal):
        """RFC 7231 allows a date, my own parser did not, and the shared one
        always has. This is what the duplication was costing."""
        when = datetime.now(UTC) + timedelta(seconds=45)
        stamp = when.strftime("%a, %d %b %Y %H:%M:%S GMT")
        face = _Face(status=503, headers={"Retry-After": stamp})
        result = _tx(journal, face).pump()
        assert result.retry_after_s and 30 <= result.retry_after_s <= 60

    def test_seconds_still_work(self, journal):
        face = _Face(status=503, headers={"Retry-After": "42"})
        assert _tx(journal, face).pump().retry_after_s == 42

    def test_header_case_does_not_matter(self, journal):
        face = _Face(status=503, headers={"RETRY-AFTER": "42"})
        assert _tx(journal, face).pump().retry_after_s == 42


class TestItPacesBeforeItIsRefused:
    def test_a_nearly_spent_budget_slows_the_next_send(self, journal):
        """The published limit is on every response, not only the refusal. A
        link that waits for a 429 has already been refused once."""
        face = _Face(headers={"X-RateLimit-Limit": "1000",
                              "X-RateLimit-Remaining": "3"})
        tx = _tx(journal, face, batch_size=2)
        tx.pump()
        assert tx.paced_until > 0

    def test_a_healthy_budget_does_not_slow_anything(self, journal):
        face = _Face(headers={"X-RateLimit-Limit": "1000",
                              "X-RateLimit-Remaining": "900"})
        tx = _tx(journal, face, batch_size=2)
        tx.pump()
        assert tx.paced_until == 0

    def test_no_headers_means_no_pacing(self, journal):
        """Never pessimize on missing data. Most faces publish nothing, and a
        transmitter that throttled itself against silence would halve every
        partner's throughput for no reason."""
        face = _Face()
        tx = _tx(journal, face, batch_size=2)
        tx.pump()
        assert tx.paced_until == 0

    def test_pacing_holds_the_next_send_without_failing_it(self, journal):
        """Pacing is not a failure: it must not consume the backoff
        escalation or mark the connection degraded."""
        face = _Face(headers={"X-RateLimit-Limit": "1000",
                              "X-RateLimit-Remaining": "1"})
        tx = _tx(journal, face, batch_size=2)
        tx.pump()
        posts = face.posts
        result = tx.pump()
        assert face.posts == posts, "sent during the pacing window"
        assert result.deferred > 0
        assert tx.connection == "ok"
        assert tx._failures == 0

    def test_the_pace_clears_once_the_budget_recovers(self, journal):
        face = _Face(headers={"X-RateLimit-Limit": "1000",
                              "X-RateLimit-Remaining": "1"})
        clock = [0.0]
        tx = _tx(journal, face, batch_size=2, clock=lambda: clock[0])
        tx.pump()
        clock[0] = tx.paced_until + 1
        face.headers = {"X-RateLimit-Limit": "1000", "X-RateLimit-Remaining": "900"}
        tx.pump()
        assert tx.paced_until == 0


class TestNothingElseChanged:
    def test_a_clean_send_still_advances(self, journal):
        face = _Face()
        tx = _tx(journal, face, batch_size=10)
        assert tx.pump().sent == 6
        assert journal.cursor(tx.cursor_name) == 6

    def test_a_two_value_transport_still_works(self, journal):
        class _Old:
            def post(self, url, body, headers):
                return 200, json.dumps({"errored": 0})

        assert _tx(journal, _Old(), batch_size=10).pump().sent == 6
