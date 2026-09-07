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

        with self._lock:
            # Parse rate limit headers (try multiple formats)
            remaining = self._parse_int_header(headers, [
                "x-ratelimit-remaining-requests",  # OpenAI
                "x-ratelimit-remaining",            # GitHub, generic
                "anthropic-ratelimit-requests-remaining",  # Anthropic
                "ratelimit-remaining",              # Standard draft
            ])

            limit = self._parse_int_header(headers, [
                "x-ratelimit-limit-requests",      # OpenAI
                "x-ratelimit-limit",                # GitHub, generic
                "anthropic-ratelimit-requests-limit",  # Anthropic
                "ratelimit-limit",                  # Standard draft
            ])

            reset_seconds = self._parse_reset_header(headers, [
                "x-ratelimit-reset-requests",      # OpenAI (e.g., "6m0s", "1s")
                "x-ratelimit-reset",                # GitHub (unix timestamp)
                "anthropic-ratelimit-requests-reset",  # Anthropic
                "ratelimit-reset",                  # Standard draft (seconds)
            ])

            # Update state
            if remaining is not None:
                self.state.remaining = remaining
            if limit is not None and limit > 0:
                self.state.limit = limit

            if reset_seconds is not None and reset_seconds > 0:
                self.state.reset_at = time.monotonic() + reset_seconds
                self.state.window_seconds = reset_seconds

            # Handle 429 — extract retry-after
            if status == 429:
                retry_after = self._parse_retry_after(headers)
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

    @staticmethod
    def _parse_int_header(headers: Any, keys: list[str]) -> int | None:
        """Try multiple header names, return first parseable int."""
        for key in keys:
            val = headers.get(key)
            if val is not None:
                try:
                    return int(val)
                except (ValueError, TypeError):
                    continue
        return None

    @staticmethod
    def _parse_reset_header(headers: Any, keys: list[str]) -> float | None:
        """Parse reset header — could be seconds, unix timestamp, or duration string."""
        for key in keys:
            val = headers.get(key)
            if val is None:
                continue
            try:
                # Try as plain seconds
                secs = float(val)
                # If it looks like a unix timestamp (>1e9), convert to relative
                if secs > 1e9:
                    return max(0, secs - time.time())
                return secs
            except (ValueError, TypeError):
                pass
            # Try OpenAI format: "6m0s", "200ms", "1s"
            return _parse_duration_string(str(val))
        return None

    @staticmethod
    def _parse_retry_after(headers: Any) -> float:
        """Parse Retry-After header (seconds or HTTP date)."""
        val = headers.get("retry-after") or headers.get("Retry-After")
        if val is None:
            return 0.0
        try:
            return float(val)
        except (ValueError, TypeError):
            return 2.0  # Default if unparseable


def _parse_duration_string(s: str) -> float | None:
    """Parse Go-style duration: '6m0s', '200ms', '1s', '1m30s'."""
    import re
    total = 0.0
    for match in re.finditer(r"(\d+(?:\.\d+)?)(ms|s|m|h)", s):
        val, unit = float(match.group(1)), match.group(2)
        if unit == "ms":
            total += val / 1000
        elif unit == "s":
            total += val
        elif unit == "m":
            total += val * 60
        elif unit == "h":
            total += val * 3600
    return total if total > 0 else None


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
