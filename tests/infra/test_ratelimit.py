# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for :mod:`axiom.infra.ratelimit`.

A transport-layer helper every ingest connector (Box, GDrive, S3,
GitHub, SharePoint) shares. Parses ``X-RateLimit-*`` and ``Retry-After``
response headers, exposes a current ``RateLimitWindow``, and provides a
``with_backoff`` decorator/context that turns a 429 + ``Retry-After``
into a clean sleep instead of a generic ``RuntimeError``.

The DP-1 stand-up (2026-06-01) made the cost explicit:
``BoxSessionApiClient`` ignored every header on every response, so the
first 429 took the run down rather than self-pacing through it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from axiom.infra.ratelimit import (
    RateLimited,
    RateLimitWindow,
    parse_headers,
    sleep_for_retry,
)


class TestRateLimited:
    def test_carries_window(self):
        w = RateLimitWindow(retry_after_s=42)
        exc = RateLimited(w)
        assert exc.window is w
        assert "42s" in str(exc)

    def test_default_message_includes_reset_when_no_retry_after(self):
        w = RateLimitWindow(reset_at=_utc(60))
        exc = RateLimited(w)
        assert "reset at" in str(exc)

    def test_custom_message(self):
        w = RateLimitWindow()
        exc = RateLimited(w, message="custom")
        assert str(exc) == "custom"


def _utc(seconds_from_now: int) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds_from_now)


class TestParseHeaders:
    def test_box_style_limit_and_remaining(self):
        h = {"X-RateLimit-Limit": "1000", "X-RateLimit-Remaining": "742"}
        w = parse_headers(h)
        assert w.limit == 1000
        assert w.remaining == 742
        assert w.reset_at is None
        assert w.retry_after_s is None

    def test_github_style_reset_timestamp(self):
        future = int((datetime.now(UTC) + timedelta(seconds=120)).timestamp())
        h = {
            "X-RateLimit-Limit": "5000",
            "X-RateLimit-Remaining": "4500",
            "X-RateLimit-Reset": str(future),
        }
        w = parse_headers(h)
        assert w.limit == 5000
        assert w.remaining == 4500
        # reset_at parsed as a tz-aware datetime within 1s of expected
        assert w.reset_at is not None
        delta = abs((w.reset_at - datetime.fromtimestamp(future, UTC)).total_seconds())
        assert delta < 1

    def test_retry_after_seconds(self):
        h = {"Retry-After": "30"}
        w = parse_headers(h)
        assert w.retry_after_s == 30

    def test_retry_after_http_date(self):
        # RFC 7231 §7.1.3 — Retry-After can be HTTP-date
        h = {"Retry-After": "Wed, 01 Jun 2026 12:00:00 GMT"}
        w = parse_headers(h)
        # Should produce a reset_at, not a seconds count
        assert w.retry_after_s is None or w.retry_after_s >= 0
        assert w.reset_at is not None

    def test_missing_headers_gives_empty_window(self):
        w = parse_headers({})
        assert w.limit is None
        assert w.remaining is None
        assert w.reset_at is None
        assert w.retry_after_s is None

    def test_case_insensitive_header_lookup(self):
        # requests' CaseInsensitiveDict and httpx headers normalize differently
        h = {"x-ratelimit-limit": "100", "x-ratelimit-remaining": "5"}
        w = parse_headers(h)
        assert w.limit == 100
        assert w.remaining == 5

    def test_malformed_values_dont_raise(self):
        # Tolerate junk; surface as None rather than crashing the connector
        h = {"X-RateLimit-Limit": "not-a-number", "Retry-After": "soon"}
        w = parse_headers(h)
        assert w.limit is None
        assert w.retry_after_s is None


class TestRateLimitWindow:
    def test_should_throttle_below_safety_margin(self):
        w = RateLimitWindow(limit=1000, remaining=5)
        # Default margin is 5% of limit, floor 10 — 5 < 10, throttle
        assert w.should_throttle() is True

    def test_should_not_throttle_when_plenty_left(self):
        w = RateLimitWindow(limit=1000, remaining=900)
        assert w.should_throttle() is False

    def test_unknown_state_does_not_throttle(self):
        # If we never saw a header, don't pessimize
        w = RateLimitWindow()
        assert w.should_throttle() is False

    def test_custom_safety_margin(self):
        w = RateLimitWindow(limit=1000, remaining=200)
        assert w.should_throttle(safety_fraction=0.30) is True
        assert w.should_throttle(safety_fraction=0.10) is False


class TestSleepForRetry:
    def test_uses_retry_after_when_present(self):
        w = RateLimitWindow(retry_after_s=12)
        slept = []
        sleep_for_retry(w, sleeper=slept.append)
        assert slept == [12]

    def test_falls_back_to_reset_at(self):
        w = RateLimitWindow(reset_at=_utc(8))
        slept = []
        sleep_for_retry(w, sleeper=slept.append)
        assert len(slept) == 1 and 6 <= slept[0] <= 9  # ~8s allowing test latency

    def test_no_signal_uses_default_backoff(self):
        w = RateLimitWindow()
        slept = []
        sleep_for_retry(w, default_backoff_s=4, sleeper=slept.append)
        assert slept == [4]

    def test_clamps_to_max(self):
        w = RateLimitWindow(retry_after_s=99999)
        slept = []
        sleep_for_retry(w, max_backoff_s=60, sleeper=slept.append)
        assert slept == [60]

    def test_floor_at_zero(self):
        # Past-dated reset must not produce negative sleep
        w = RateLimitWindow(reset_at=_utc(-30))
        slept = []
        sleep_for_retry(w, sleeper=slept.append)
        assert slept == [0]


class TestThereIsOnlyOneParser:
    """The DRY guard. This vocabulary had five implementations across the
    tree — two in infra, three in notification channels, and briefly a sixth
    in the DAQ transmitter — and they did not agree: only one understood the
    HTTP-date form, only one knew the vendor aliases, and the one a partner's
    node ran understood the least.

    It checks for a PARSER being defined, not for the header being mentioned.
    Naming `Retry-After` to pull it off a response is fine and necessary;
    turning the string into a number is the part that must happen once.
    """

    #: The module that is allowed to do the parsing.
    PARSER = "src/axiom/infra/ratelimit.py"

    #: Function names that mean somebody built their own. Each was a real one.
    PARSER_DEFS = (
        "def _parse_retry_after",
        "def _parse_int_header",
        "def _parse_reset_header",
        "def _parse_duration_string",
        "def _duration_to_seconds",
        "def _retry_after",
    )

    #: The stdlib call that turns an HTTP-date into a datetime. Outside the
    #: parser it means a second date implementation, which is the capability
    #: the copies most often lacked and most often got wrong.
    DATE_PARSE = "parsedate_to_datetime"

    def _modules(self):
        import pathlib as _p

        root = _p.Path(__file__).resolve().parents[2] / "src" / "axiom"
        for path in root.rglob("*.py"):
            rel = path.relative_to(root.parents[1]).as_posix()
            if "/tests/" in f"/{rel}" or rel == self.PARSER:
                continue
            yield rel, path.read_text(encoding="utf-8", errors="replace")

    def test_nobody_else_defines_one(self):
        offenders = []
        for rel, text in self._modules():
            for marker in self.PARSER_DEFS:
                if marker in text:
                    offenders.append(f"{rel}: {marker}")
        assert not offenders, (
            "these define their own rate-limit parser; delegate to "
            "axiom.infra.ratelimit instead: " + "; ".join(sorted(offenders))
        )

    def test_nobody_else_parses_an_http_date(self):
        offenders = [rel for rel, text in self._modules() if self.DATE_PARSE in text]
        assert not offenders, sorted(offenders)

    def test_the_guard_can_fail(self):
        """A negative control: the markers are real strings that would match."""
        assert any(m in "def _parse_retry_after(value):" for m in self.PARSER_DEFS)

    def test_the_parser_still_handles_what_the_copies_did(self):
        """Every capability the removed copies had, kept."""
        from axiom.infra.ratelimit import duration_to_seconds, retry_after_seconds

        assert retry_after_seconds("42") == 42          # all three channels
        assert retry_after_seconds("0") is None         # never a zero wait
        assert retry_after_seconds("nonsense") is None
        assert retry_after_seconds(None) is None
        assert retry_after_seconds("120", cap=60) == 60
        assert duration_to_seconds("6m0s") == 360.0     # the orphaned copy
        assert duration_to_seconds("200ms") == 0.2
