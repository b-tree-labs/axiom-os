# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""When a deferred send is retried, and why the old answer was wrong twice.

Two faults, both about the retry timer, both with well-worn fixes.

**The host's own answer was ignored.** `TransmitResult` has carried a
`retry_after_s` field the whole time and it was only ever filled from our own
backoff table. A face answering 429 or 503 with a `Retry-After` header is
telling us precisely when it will be ready, and we retried on our schedule
instead — which is the one case where the sender has better information
available and does not use it.

**The backoff had no jitter.** `(1, 2, 4, 8, 16, 32, 60)` is fixed, so every
site that was mid-send when a host went down comes back at the same second,
and again at the same second, for as long as the outage lasts. Each recovery
attempt is a small synchronised flood against a host that has just restarted.
Decorrelated jitter is the standard fix and it is a few lines.

The transport returns `(status, text)` and has no headers, so honouring
`Retry-After` needs the transport to hand them over. It is widened rather than
replaced: a transport that returns two values still works and simply cannot
report a header, which is the honest behaviour for one that never saw it.
"""

from __future__ import annotations

import json

import pytest

from ..consolidator import DAQConsolidator
from ..envelope import ConsolidatedRecord
from ..journal import DAQJournal
from ..transmitter import DEFAULT_BACKOFF, DAQTransmitter


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
    for i in range(3):
        cons.consume(_rec(i))
    return j


class _Face:
    """A face that answers with a status and optionally some headers."""

    def __init__(self, status=503, body="busy", headers=None):
        self.status, self.body, self.headers = status, body, headers

    def post(self, url, body, headers):
        if self.headers is None:
            return self.status, self.body
        return self.status, self.body, self.headers


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


class TestTheHostsOwnAnswerWins:
    def test_retry_after_seconds_is_honoured(self, journal):
        tx = _tx(journal, _Face(headers={"Retry-After": "42"}), jitter=None)
        assert tx.pump().retry_after_s == 42

    def test_it_beats_our_table(self, journal):
        """Our first backoff step is a second. A host saying 42 knows better."""
        tx = _tx(journal, _Face(headers={"Retry-After": "42"}), jitter=None)
        assert tx.pump().retry_after_s != DEFAULT_BACKOFF[0]

    def test_the_header_name_is_matched_case_insensitively(self, journal):
        tx = _tx(journal, _Face(headers={"retry-after": "30"}), jitter=None)
        assert tx.pump().retry_after_s == 30

    def test_a_nonsense_value_falls_back_to_our_table(self, journal):
        """A host date-formatted or garbled header must not become a zero
        wait, which would turn politeness into a hot loop."""
        tx = _tx(journal, _Face(headers={"Retry-After": "next tuesday"}), jitter=None)
        assert tx.pump().retry_after_s == DEFAULT_BACKOFF[0]

    def test_a_negative_value_falls_back(self, journal):
        tx = _tx(journal, _Face(headers={"Retry-After": "-5"}), jitter=None)
        assert tx.pump().retry_after_s == DEFAULT_BACKOFF[0]

    def test_an_absurd_value_is_capped(self, journal):
        """A host asking for a day would silence a site until somebody
        noticed. Respected up to a ceiling, then ours."""
        tx = _tx(journal, _Face(headers={"Retry-After": "86400"}), jitter=None)
        assert tx.pump().retry_after_s <= 3600

    def test_a_transport_with_no_headers_still_works(self, journal):
        """The seam is widened, not replaced. A two-value transport is a
        transport that never saw a header, and it keeps working."""
        tx = _tx(journal, _Face(), jitter=None)
        assert tx.pump().retry_after_s == DEFAULT_BACKOFF[0]


class TestTheBackoffIsJittered:
    def test_two_producers_do_not_come_back_at_the_same_instant(self, journal, tmp_path):
        """The failure this prevents: every site that was mid-send when a host
        went down retrying in lockstep, for the whole outage."""
        waits = set()
        for i in range(12):
            j = DAQJournal(tmp_path / f"j{i}")
            cons = DAQConsolidator(journal=j, producer_id=f"p{i}", feed="loop")
            cons.consume(_rec(0))
            waits.add(_tx(j, _Face()).pump().retry_after_s)
        assert len(waits) > 1, waits

    def test_the_wait_stays_within_the_step(self, journal):
        """Jitter spreads the herd; it must not extend an outage. Never more
        than the step it is jittering."""
        for _ in range(20):
            tx = _tx(journal, _Face())
            wait = tx.pump().retry_after_s
            assert 0 < wait <= DEFAULT_BACKOFF[0], wait

    def test_it_never_returns_zero(self, journal):
        """A zero wait is a hot loop against a host that just failed."""
        for _ in range(20):
            assert _tx(journal, _Face()).pump().retry_after_s > 0

    def test_it_can_be_turned_off_for_a_deterministic_test(self, journal):
        assert _tx(journal, _Face(), jitter=None).pump().retry_after_s == DEFAULT_BACKOFF[0]

    def test_a_host_supplied_wait_is_not_jittered(self, journal):
        """It is an instruction, not an estimate."""
        tx = _tx(journal, _Face(headers={"Retry-After": "42"}))
        assert tx.pump().retry_after_s == 42


class TestTheEscalationStillEscalates:
    def test_repeated_failures_walk_up_the_table(self, journal):
        tx = _tx(journal, _Face(), jitter=None)
        waits = []
        for _ in range(4):
            tx._not_before = 0  # skip the sleep; we are testing the schedule
            waits.append(tx.pump().retry_after_s)
        assert waits == list(DEFAULT_BACKOFF[:4])

    def test_a_success_resets_it(self, journal):
        face = _Face()
        tx = _tx(journal, face, jitter=None)
        tx.pump()
        face.status, face.body = 200, json.dumps({"errored": 0})
        tx._not_before = 0
        tx.pump()
        assert tx._failures == 0


class TestTheRealTransportCarriesTheHeader:
    """The fix above is worthless if the shipped transport drops the header,
    and that is exactly what it did: urllib delivers a 503 as an exception,
    and the header a host sends is on the exception.

    A mechanism nothing consults is a mechanism nobody built, so this asserts
    against `UrllibTransport` itself rather than a fake.
    """

    def _transport(self, response):
        """The real transport with its opener substituted.

        `_opener` is the seam the class already has for a custom CA bundle;
        setting it directly is how a test drives the real code path without a
        socket."""
        from ..transmitter import UrllibTransport

        class _Opener:
            def open(self, req, timeout=None):
                return response

        transport = UrllibTransport()
        transport._opener = _Opener()
        return transport

    def test_a_success_carries_its_headers(self):
        class _Resp:
            status = 200
            headers = {"Content-Type": "application/json"}

            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        answer = self._transport(_Resp()).post("http://x/y", b"{}", {})
        assert len(answer) == 3
        assert answer[2]["Content-Type"] == "application/json"

    def test_a_503_carries_retry_after(self):
        """The branch that matters. This is where a host says when to return
        and where the value was being thrown away."""
        import urllib.error

        from ..transmitter import UrllibTransport, _honoured_retry_after, _rate_limit_window

        class _Opener:
            def open(self, req, timeout=None):
                raise urllib.error.HTTPError(
                    "http://x/y", 503, "busy",
                    {"Retry-After": "42"}, None,
                )

        transport = UrllibTransport()
        transport._opener = _Opener()
        status, _text, headers = transport.post("http://x/y", b"{}", {})
        assert status == 503
        assert _honoured_retry_after(_rate_limit_window(headers)) == 42

    def test_a_response_with_no_headers_still_answers(self):
        class _Resp:
            status = 200
            headers = None

            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        status, text, headers = self._transport(_Resp()).post("http://x/y", b"{}", {})
        assert status == 200 and headers == {}
