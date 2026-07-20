# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the adaptive rate limiter."""

from __future__ import annotations

import time
from unittest import mock

import pytest

from axiom.infra.rate_limiter import (
    AdaptiveRateLimiter,
    _parse_duration_string,
    get_limiter,
    reset_limiters,
)


class TestDurationParsing:
    def test_seconds(self):
        assert _parse_duration_string("5s") == 5.0

    def test_milliseconds(self):
        assert _parse_duration_string("200ms") == 0.2

    def test_minutes(self):
        assert _parse_duration_string("6m0s") == 360.0

    def test_compound(self):
        assert _parse_duration_string("1m30s") == 90.0

    def test_empty(self):
        assert _parse_duration_string("") is None

    def test_no_units(self):
        assert _parse_duration_string("hello") is None


class TestAdaptiveRateLimiter:
    def test_no_limit_known_no_wait(self):
        limiter = AdaptiveRateLimiter("test")
        waited = limiter.wait()
        assert waited == 0.0

    def test_learns_from_response_headers(self):
        limiter = AdaptiveRateLimiter("test")
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.headers = {
            "x-ratelimit-remaining-requests": "50",
            "x-ratelimit-limit-requests": "60",
            "x-ratelimit-reset-requests": "60s",
        }
        limiter.update(resp)
        assert limiter.state.remaining == 50
        assert limiter.state.limit == 60
        assert limiter.state.min_interval > 0

    def test_computes_pacing_interval(self):
        limiter = AdaptiveRateLimiter("test", headroom=0.1)
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.headers = {
            "x-ratelimit-limit-requests": "60",
            "x-ratelimit-remaining-requests": "55",
            "x-ratelimit-reset-requests": "60s",
        }
        limiter.update(resp)
        # 60 * 0.9 = 54 safe requests per 60s = 1.11s interval
        assert 1.0 < limiter.state.min_interval < 1.2

    def test_429_sets_retry_after(self):
        limiter = AdaptiveRateLimiter("test")
        resp = mock.MagicMock()
        resp.status_code = 429
        resp.headers = {"retry-after": "5"}
        limiter.update(resp)
        assert limiter.state.retry_after == 5.0

    def test_github_headers(self):
        limiter = AdaptiveRateLimiter("github")
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.headers = {
            "x-ratelimit-remaining": "4999",
            "x-ratelimit-limit": "5000",
            "x-ratelimit-reset": str(int(time.time()) + 3600),
        }
        limiter.update(resp)
        assert limiter.state.remaining == 4999
        assert limiter.state.limit == 5000

    def test_anthropic_headers(self):
        limiter = AdaptiveRateLimiter("anthropic")
        resp = mock.MagicMock()
        resp.status_code = 200
        resp.headers = {
            "anthropic-ratelimit-requests-remaining": "990",
            "anthropic-ratelimit-requests-limit": "1000",
        }
        limiter.update(resp)
        assert limiter.state.remaining == 990
        assert limiter.state.limit == 1000

    def test_utilization(self):
        limiter = AdaptiveRateLimiter("test")
        limiter.state.limit = 100
        limiter.state.remaining = 25
        assert limiter.state.utilization == pytest.approx(0.75)

    def test_utilization_unknown(self):
        limiter = AdaptiveRateLimiter("test")
        assert limiter.state.utilization == 0.0


class TestGlobalRegistry:
    def test_get_creates_new(self):
        reset_limiters()
        limiter = get_limiter("new_svc")
        assert limiter.name == "new_svc"

    def test_get_returns_same(self):
        reset_limiters()
        a = get_limiter("same_svc")
        b = get_limiter("same_svc")
        assert a is b

    def test_reset_clears(self):
        reset_limiters()
        get_limiter("x")
        reset_limiters()
        # New instance after reset
        new = get_limiter("x")
        assert new.state.limit == -1
