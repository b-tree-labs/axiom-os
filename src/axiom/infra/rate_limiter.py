# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Adaptive rate limiter — stays just below each connection's actual limit.

Reads standard rate limit headers from API responses and dynamically
adjusts request pacing. Never hardcodes limits — learns them from the
API itself and stays 10% below the observed threshold.

Usage:
    from axiom.infra.rate_limiter import get_limiter

    limiter = get_limiter("openai")
    limiter.wait()              # Block until safe to send
    response = requests.post(...)
    limiter.update(response)    # Learn from response headers

Supported header formats:
- OpenAI:    x-ratelimit-remaining-requests, x-ratelimit-reset-requests
- GitHub:    x-ratelimit-remaining, x-ratelimit-reset
- Anthropic: anthropic-ratelimit-requests-remaining, anthropic-ratelimit-requests-reset
- Generic:   retry-after (seconds or HTTP date)
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class RateLimitState:
    """Observed rate limit state for a connection."""
    remaining: int = -1          # Requests remaining in window (-1 = unknown)
    limit: int = -1              # Total requests allowed per window (-1 = unknown)
    reset_at: float = 0.0       # time.monotonic() when window resets
    retry_after: float = 0.0    # Seconds to wait (from 429 response)
    last_request_at: float = 0.0
    min_interval: float = 0.0   # Computed: seconds between requests to stay safe
    window_seconds: float = 60.0  # Assumed window duration

    @property
    def is_known(self) -> bool:
        return self.limit > 0

    @property
    def utilization(self) -> float:
        """How much of the rate limit window has been consumed (0.0-1.0)."""
        if self.limit <= 0:
            return 0.0
        return 1.0 - (self.remaining / self.limit)


class AdaptiveRateLimiter:
    """Per-connection adaptive rate limiter.

    Reads rate limit headers from responses and paces requests to stay
    at 90% of the observed limit (configurable via headroom).
    """

    def __init__(self, name: str, headroom: float = 0.1):
        """
        Args:
            name: Connection name for logging.
            headroom: Fraction of capacity to reserve (0.1 = stay 10% below limit).
        """
        self.name = name
        self.headroom = headroom
        self.state = RateLimitState()
        self._lock = threading.Lock()

    def wait(self) -> float:
        """Block until it's safe to send the next request.

        Returns the number of seconds waited (0.0 if no wait needed).
        """
        with self._lock:
            now = time.monotonic()
            wait_time = 0.0

            # If we got a retry-after from a 429, respect it
            if self.state.retry_after > 0:
                wait_until = self.state.last_request_at + self.state.retry_after
                if now < wait_until:
                    wait_time = wait_until - now

            # If we know the rate limit, pace ourselves
            elif self.state.min_interval > 0:
                next_allowed = self.state.last_request_at + self.state.min_interval
                if now < next_allowed:
                    wait_time = next_allowed - now

            # If remaining is critically low, slow down
            elif self.state.remaining >= 0 and self.state.remaining <= 2:
                # Wait until reset
                if self.state.reset_at > now:
                    wait_time = self.state.reset_at - now
                else:
                    wait_time = 1.0  # Conservative 1s wait

        if wait_time > 0:
            log.debug(
                "Rate limiter [%s]: waiting %.1fs (remaining=%d, limit=%d)",
                self.name, wait_time, self.state.remaining, self.state.limit,
            )
            time.sleep(wait_time)

        with self._lock:
            self.state.last_request_at = time.monotonic()

        return wait_time

    def update(self, response: Any) -> None:
        """Update state from response headers. Call after every API request.

        Accepts any object with a .headers dict-like and .status_code int.
        """
        headers = getattr(response, "headers", {})
        status = getattr(response, "status_code", 200)

        # ONE parser for this vocabulary, in `axiom.infra.ratelimit`. This
        # method used to carry its own three — `_parse_int_header`,
        # `_parse_reset_header`, `_parse_retry_after` — over the same header
        # names, and the DAQ transmitter briefly grew a fourth. Three
        # implementations of "when does the server want me back" is three
        # chances to disagree, and the loser is whichever one happens to be
        # running on somebody else's machine.
        from axiom.infra.ratelimit import parse_headers

        window = parse_headers(headers)

        with self._lock:
            remaining = window.remaining
            limit = window.limit
            reset_seconds: float | None = None
            if window.reset_at is not None:
                from datetime import UTC, datetime

                reset_seconds = (window.reset_at - datetime.now(UTC)).total_seconds()

            # Update state
            if remaining is not None:
                self.state.remaining = remaining
            if limit is not None and limit > 0:
                self.state.limit = limit

            if reset_seconds is not None and reset_seconds > 0:
                self.state.reset_at = time.monotonic() + reset_seconds
                self.state.window_seconds = reset_seconds

            # Handle 429 — the server's own directive
            if status == 429:
                retry_after = float(window.retry_after_s or 0)
                if retry_after <= 0 and reset_seconds and reset_seconds > 0:
                    # An HTTP-date `Retry-After` lands in `reset_at`, which is
                    # still the server telling us when to come back.
                    retry_after = reset_seconds
                self.state.retry_after = retry_after if retry_after > 0 else 2.0
                # Record throttle in usage tracking
                try:
                    from axiom.infra.connections import record_usage
                    record_usage(self.name, 0, throttled=True)
                except Exception:
                    pass
            else:
                self.state.retry_after = 0.0

            # Compute optimal pacing interval
            if self.state.limit > 0 and self.state.window_seconds > 0:
                # Target: (1 - headroom) * limit requests per window
                safe_limit = self.state.limit * (1.0 - self.headroom)
                if safe_limit > 0:
                    self.state.min_interval = self.state.window_seconds / safe_limit
                    log.debug(
                        "Rate limiter [%s]: %d/%d remaining, pacing at %.2fs/req",
                        self.name, self.state.remaining, self.state.limit,
                        self.state.min_interval,
                    )


# Duration parsing lives in `axiom.infra.ratelimit`, which is the one parser
# for this whole vocabulary. This module had its own copy; the shared one now
# carries its behaviour, including the "200ms" form the other lacked. Re-
# exported under the old name so existing importers keep working.
#
# The alias is the load-bearing half of that sentence and it had gone missing:
# a lint pass removed it as an unused name, which left the comment promising
# something the module no longer did. Nothing here noticed, because nothing
# here imports it — the importer is a test in another package, and it failed
# at collection with an ImportError rather than as a readable assertion.
from axiom.infra.ratelimit import duration_to_seconds as _parse_duration_string  # noqa: E402

__all__ = [*globals().get("__all__", []), "_parse_duration_string"]

# ---------------------------------------------------------------------------
# Global limiter registry
# ---------------------------------------------------------------------------

_limiters: dict[str, AdaptiveRateLimiter] = {}
_registry_lock = threading.Lock()


def get_limiter(name: str, headroom: float = 0.1) -> AdaptiveRateLimiter:
    """Get or create an adaptive rate limiter for a connection."""
    with _registry_lock:
        if name not in _limiters:
            _limiters[name] = AdaptiveRateLimiter(name, headroom=headroom)
        return _limiters[name]


def reset_limiters() -> None:
    """Reset all rate limiters (for testing)."""
    with _registry_lock:
        _limiters.clear()
