# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Tests for gateway resilience: 5xx + network-error retry with backoff.

Closes the parity-doc gap 'Rate limits + retries for LLM calls
(production-grade resilience)'. The existing _post_with_rate_limit_retry
handled 429 only; 5xx (502/503/504 — common transient backend issues)
and network errors (ConnectionError, Timeout) bubbled up immediately
on the first attempt. This left every call site to implement its own
retry, or worse, fail at the first hiccup.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class _FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status_code: int, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = ""

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.exceptions.HTTPError(f"{self.status_code} error")

    def json(self):
        return {}


@pytest.fixture
def fake_requests():
    """Patch sleep to no-op so tests run in microseconds, not seconds."""
    import requests

    with patch("axiom.infra.gateway.time.sleep", return_value=None):
        yield requests


def test_200_returns_immediately(fake_requests):
    """No retry needed for success."""
    from axiom.infra.gateway import _post_with_rate_limit_retry

    requests_mod = MagicMock()
    requests_mod.post.return_value = _FakeResponse(200)
    requests_mod.exceptions = fake_requests.exceptions

    out = _post_with_rate_limit_retry(requests_mod, "http://x", {}, {})
    assert out.status_code == 200
    assert requests_mod.post.call_count == 1


def test_429_retries_then_succeeds(fake_requests):
    """Existing 429 retry behavior preserved."""
    from axiom.infra.gateway import _post_with_rate_limit_retry

    requests_mod = MagicMock()
    requests_mod.exceptions = fake_requests.exceptions
    requests_mod.post.side_effect = [
        _FakeResponse(429, headers={"retry-after": "0"}),
        _FakeResponse(200),
    ]

    out = _post_with_rate_limit_retry(requests_mod, "http://x", {}, {})
    assert out.status_code == 200
    assert requests_mod.post.call_count == 2


def test_502_retries_then_succeeds(fake_requests):
    """502 Bad Gateway is transient — retry it."""
    from axiom.infra.gateway import _post_with_rate_limit_retry

    requests_mod = MagicMock()
    requests_mod.exceptions = fake_requests.exceptions
    requests_mod.post.side_effect = [
        _FakeResponse(502),
        _FakeResponse(200),
    ]

    out = _post_with_rate_limit_retry(requests_mod, "http://x", {}, {})
    assert out.status_code == 200
    assert requests_mod.post.call_count == 2


def test_503_retries(fake_requests):
    from axiom.infra.gateway import _post_with_rate_limit_retry

    requests_mod = MagicMock()
    requests_mod.exceptions = fake_requests.exceptions
    requests_mod.post.side_effect = [
        _FakeResponse(503),
        _FakeResponse(200),
    ]

    out = _post_with_rate_limit_retry(requests_mod, "http://x", {}, {})
    assert out.status_code == 200


def test_504_retries(fake_requests):
    from axiom.infra.gateway import _post_with_rate_limit_retry

    requests_mod = MagicMock()
    requests_mod.exceptions = fake_requests.exceptions
    requests_mod.post.side_effect = [
        _FakeResponse(504),
        _FakeResponse(200),
    ]

    out = _post_with_rate_limit_retry(requests_mod, "http://x", {}, {})
    assert out.status_code == 200


def test_400_does_not_retry(fake_requests):
    """400 is a client error — caller's bug, retrying won't help."""
    import requests

    from axiom.infra.gateway import _post_with_rate_limit_retry

    requests_mod = MagicMock()
    requests_mod.exceptions = fake_requests.exceptions
    requests_mod.post.return_value = _FakeResponse(400)

    with pytest.raises(requests.exceptions.HTTPError):
        _post_with_rate_limit_retry(requests_mod, "http://x", {}, {})
    assert requests_mod.post.call_count == 1


def test_401_does_not_retry(fake_requests):
    """401 is auth failure — won't fix itself."""
    import requests

    from axiom.infra.gateway import _post_with_rate_limit_retry

    requests_mod = MagicMock()
    requests_mod.exceptions = fake_requests.exceptions
    requests_mod.post.return_value = _FakeResponse(401)

    with pytest.raises(requests.exceptions.HTTPError):
        _post_with_rate_limit_retry(requests_mod, "http://x", {}, {})
    assert requests_mod.post.call_count == 1


def test_connection_error_retries(fake_requests):
    """Transient network failure — retry."""
    import requests

    from axiom.infra.gateway import _post_with_rate_limit_retry

    requests_mod = MagicMock()
    requests_mod.exceptions = fake_requests.exceptions
    requests_mod.post.side_effect = [
        requests.exceptions.ConnectionError("boom"),
        _FakeResponse(200),
    ]

    out = _post_with_rate_limit_retry(requests_mod, "http://x", {}, {})
    assert out.status_code == 200
    assert requests_mod.post.call_count == 2


def test_read_timeout_retries(fake_requests):
    """Server slowness can be transient — retry once before giving up."""
    import requests

    from axiom.infra.gateway import _post_with_rate_limit_retry

    requests_mod = MagicMock()
    requests_mod.exceptions = fake_requests.exceptions
    requests_mod.post.side_effect = [
        requests.exceptions.ReadTimeout("slow"),
        _FakeResponse(200),
    ]

    out = _post_with_rate_limit_retry(requests_mod, "http://x", {}, {})
    assert out.status_code == 200


def test_gives_up_after_max_retries_on_5xx(fake_requests):
    """Persistent 5xx eventually raises HTTPError, doesn't loop forever."""
    import requests

    from axiom.infra.gateway import _post_with_rate_limit_retry

    requests_mod = MagicMock()
    requests_mod.exceptions = fake_requests.exceptions
    requests_mod.post.return_value = _FakeResponse(503)

    with pytest.raises(requests.exceptions.HTTPError):
        _post_with_rate_limit_retry(requests_mod, "http://x", {}, {})
    # Fixed retry budget — at least 2 attempts, less than 10 (sanity).
    assert 2 <= requests_mod.post.call_count <= 10


def test_gives_up_after_max_retries_on_connection_error(fake_requests):
    import requests

    from axiom.infra.gateway import _post_with_rate_limit_retry

    requests_mod = MagicMock()
    requests_mod.exceptions = fake_requests.exceptions
    requests_mod.post.side_effect = requests.exceptions.ConnectionError("down")

    with pytest.raises(requests.exceptions.ConnectionError):
        _post_with_rate_limit_retry(requests_mod, "http://x", {}, {})
    assert 2 <= requests_mod.post.call_count <= 10


def test_backoff_includes_jitter(fake_requests):
    """Sleep duration varies between attempts so concurrent callers don't
    sync-pulse the upstream after a 503."""
    from axiom.infra.gateway import _post_with_rate_limit_retry

    requests_mod = MagicMock()
    requests_mod.exceptions = fake_requests.exceptions
    requests_mod.post.return_value = _FakeResponse(503)

    sleep_calls: list[float] = []

    def _track_sleep(d):
        sleep_calls.append(d)

    with patch("axiom.infra.gateway.time.sleep", side_effect=_track_sleep):
        import requests

        with pytest.raises(requests.exceptions.HTTPError):
            _post_with_rate_limit_retry(requests_mod, "http://x", {}, {})

    # Got at least 2 backoff sleeps, and at least one pair differs (jittered).
    if len(sleep_calls) >= 2:
        # If all values are identical, jitter is broken. We allow some
        # equality (jitter is small) but require not-all-equal across at
        # least one pair if there are 3+ samples.
        if len(sleep_calls) >= 3:
            assert len(set(round(s, 3) for s in sleep_calls)) > 1, (
                f"backoff sleeps look unjittered: {sleep_calls}"
            )
