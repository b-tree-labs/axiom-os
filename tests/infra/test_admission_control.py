# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Admission control, and the reason it shares a module with the parser.

Everything else in `axiom.infra.ratelimit` reads somebody else's limit. This
writes ours, and it lives beside the parser on purpose: a limiter publishing
headers that a different module parses is two halves of one protocol that can
drift apart, and this file exists because there were three parsers of that
vocabulary before there was one.

So the test that matters most here is not the arithmetic. It is that our own
parser reads exactly what our own emitter writes.
"""

from __future__ import annotations

import pytest

from axiom.infra.ratelimit import (
    CANONICAL_LIMIT_HEADER,
    CANONICAL_REMAINING_HEADER,
    CANONICAL_RESET_HEADER,
    RETRY_AFTER_HEADER,
    BucketRegistry,
    TokenBucket,
    parse_headers,
)


@pytest.fixture
def clock():
    return [1000.0]


@pytest.fixture
def registry(clock):
    return BucketRegistry(capacity=3, refill_per_second=1.0, clock=lambda: clock[0])


class TestTheLoopCloses:
    """The point of putting emitter and parser in one module."""

    def test_our_parser_reads_our_emitter(self, registry):
        _admitted, headers = registry.check("site-b")
        window = parse_headers(headers)
        assert window.limit == 3
        assert window.remaining is not None
        assert window.reset_at is not None

    def test_a_refusal_carries_a_wait_our_parser_understands(self, registry):
        for _ in range(4):
            admitted, headers = registry.check("site-b")
        assert not admitted
        assert parse_headers(headers).retry_after_s >= 1

    def test_the_names_are_the_ones_the_parser_looks_for(self):
        from axiom.infra.ratelimit import (
            LIMIT_HEADERS,
            REMAINING_HEADERS,
            RESET_HEADERS,
        )

        assert CANONICAL_LIMIT_HEADER in LIMIT_HEADERS
        assert CANONICAL_REMAINING_HEADER in REMAINING_HEADERS
        assert CANONICAL_RESET_HEADER in RESET_HEADERS

    def test_the_reset_is_an_epoch_not_a_duration(self, registry):
        """The parser reads this name as an epoch. A duration under it would
        be read as a 1970 timestamp and the caller would never wait."""
        _admitted, headers = registry.check("site-b")
        assert int(headers[CANONICAL_RESET_HEADER]) > 1_600_000_000


class TestItAdmitsAndThenRefuses:
    def test_a_burst_up_to_capacity_is_admitted(self, registry):
        assert all(registry.check("site-b")[0] for _ in range(3))

    def test_the_next_one_is_refused(self, registry):
        for _ in range(3):
            registry.check("site-b")
        assert registry.check("site-b")[0] is False

    def test_waiting_earns_another(self, registry, clock):
        for _ in range(3):
            registry.check("site-b")
        clock[0] += 1.0
        assert registry.check("site-b")[0] is True

    def test_a_caller_under_the_rate_never_notices(self, registry, clock):
        for _ in range(20):
            clock[0] += 1.0
            assert registry.check("site-b")[0] is True

    def test_the_budget_does_not_grow_past_capacity(self, registry, clock):
        clock[0] += 10_000
        assert all(registry.check("site-b")[0] for _ in range(3))
        assert registry.check("site-b")[0] is False


class TestOneCallerCannotSpendAnothersBudget:
    def test_buckets_are_per_key(self, registry):
        for _ in range(3):
            registry.check("site-b")
        assert registry.check("site-b")[0] is False
        assert registry.check("site-c")[0] is True

    def test_exhausting_one_leaves_the_other_whole(self, registry):
        for _ in range(5):
            registry.check("site-b")
        assert registry.check("site-c")[1][CANONICAL_REMAINING_HEADER] == "2"


class TestTheHeadersAreAlwaysPublished:
    def test_an_admitted_request_still_carries_the_budget(self, registry):
        """A caller that only learns its budget when refused has already been
        refused once. Ours pace on the published number."""
        admitted, headers = registry.check("site-b")
        assert admitted
        assert CANONICAL_REMAINING_HEADER in headers

    def test_only_a_refusal_carries_retry_after(self, registry):
        _admitted, headers = registry.check("site-b")
        assert RETRY_AFTER_HEADER not in headers

    def test_retry_after_is_never_zero(self, registry):
        """A zero invites an immediate retry, which is how a limiter becomes
        a hot loop instead of a limit."""
        for _ in range(9):
            admitted, headers = registry.check("site-b")
            if not admitted:
                assert int(headers[RETRY_AFTER_HEADER]) >= 1


class TestTheBucketItself:
    def test_a_window_would_allow_a_double_burst_and_a_bucket_does_not(self):
        """The reason this is a bucket. A fixed window lets a caller spend
        its whole budget at the end of one window and again at the start of
        the next, which is the burst the limit exists to prevent."""
        bucket = TokenBucket(capacity=5, refill_per_second=5 / 60)
        now = 0.0
        assert sum(bucket.take(now) for _ in range(5)) == 5
        now = 59.9
        assert bucket.take(now) is False

    def test_capacity_below_one_is_raised_to_one(self):
        """A bucket that can never admit anything is a misconfiguration that
        looks like an outage."""
        assert TokenBucket(capacity=0, refill_per_second=1).take(0.0) is True

    def test_seconds_until_is_zero_when_tokens_exist(self):
        assert TokenBucket(capacity=2, refill_per_second=1).seconds_until() == 0.0
