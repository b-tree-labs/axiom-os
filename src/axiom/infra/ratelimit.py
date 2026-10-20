# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Transport-layer rate-limit helper for ingest connectors.

Every ingest connector (Box, GDrive, S3, GitHub, SharePoint) shares
the same response-header → throttle-decision shape. The DP-1 self-hosted
node stand-up (2026-06-01) made the cost of NOT having this explicit:
``BoxSessionApiClient`` ignored every ``X-RateLimit-*`` and
``Retry-After`` on every response, so the first 429 took the whole run
down rather than self-pacing through it.

Surface:

- :class:`RateLimitWindow` — a snapshot of the current limit, remaining
  budget, reset time, and any explicit ``Retry-After`` directive.
- :func:`parse_headers` — header dict → ``RateLimitWindow``; case-
  insensitive, tolerant of malformed values (surface as ``None``, never
  raise inside a connector's hot path).
- :func:`sleep_for_retry` — pick the right wait from ``Retry-After`` /
  ``reset_at`` / a configurable default; clamp to a sane max; never go
  negative.

Connectors compose these into their own retry policy. This module owns
the parsing, not the policy — the policy lives next to the connector.

Header support reflects what the major SaaS APIs actually send:

- **Box** — ``X-RateLimit-Limit``, ``X-RateLimit-Remaining``,
  ``X-RateLimit-Reset`` (epoch seconds), ``Retry-After`` on 429.
- **GitHub** — same four, ``Retry-After`` on secondary limits.
- **Google Drive** — uses a 403 with reason rather than 429; callers
  pass synthetic headers.
- **Slack** — ``Retry-After`` only.
- **S3** — ``x-amz-request-id`` + slowdown headers; callers parse
  the slowdown into a synthetic ``Retry-After``.
"""

from __future__ import annotations

import email.utils
import re as _re
import math as _math
import threading as _threading
import time as _time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RateLimitWindow:
    """Snapshot of a connector's current rate-limit state.

    All fields optional — a connector that has not yet seen a response
    starts with an empty window and refuses to pessimize. After the
    first response, the parsed fields drive throttling decisions.
    """

    limit: int | None = None
    remaining: int | None = None
    reset_at: datetime | None = None
    retry_after_s: int | None = None

    def should_throttle(self, *, safety_fraction: float = 0.05,
                        floor: int = 10) -> bool:
        """Whether the caller should pace down before the next call.

        Returns ``True`` if ``remaining`` has fallen below a safety
        margin (default 5% of ``limit``, or ``floor`` calls, whichever
        is larger). Returns ``False`` if we don't have enough info to
        decide — never pessimize on missing data.
        """
        if self.limit is None or self.remaining is None:
            return False
        margin = max(int(self.limit * safety_fraction), floor)
        return self.remaining < margin


# ---------------------------------------------------------------------------
# Header parsing
# ---------------------------------------------------------------------------


def _ci_get(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive lookup over an arbitrary header mapping."""
    lower = name.lower()
    for k, v in headers.items():
        if k.lower() == lower:
            return v
    return None


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


#: Header names that mean the same thing, most standard first.
#:
#: One list, because there were two. `infra.rate_limiter` carried its own
#: `_parse_int_header`, `_parse_reset_header` and `_parse_retry_after` over
#: this same vocabulary, and the DAQ transmitter briefly grew a third that
#: understood less than either. Three parsers for "when does the server want
#: me back" is three chances to disagree, and the caller who loses is whichever
#: one is running on somebody else's machine.
LIMIT_HEADERS: tuple[str, ...] = (
    "X-RateLimit-Limit",
    "X-RateLimit-Limit-Requests",       # OpenAI
    "Anthropic-RateLimit-Requests-Limit",
)
REMAINING_HEADERS: tuple[str, ...] = (
    "X-RateLimit-Remaining",
    "X-RateLimit-Remaining-Requests",   # OpenAI
    "Anthropic-RateLimit-Requests-Remaining",
)
RESET_HEADERS: tuple[str, ...] = (
    "X-RateLimit-Reset",
    "X-RateLimit-Reset-Requests",       # OpenAI
    "Anthropic-RateLimit-Requests-Reset",
)


def _first(headers: Mapping[str, str], names: tuple[str, ...]) -> str | None:
    """The first of *names* the response actually carries."""
    for name in names:
        value = _ci_get(headers, name)
        if value is not None:
            return value
    return None



#: A Go-style duration: "6m0s", "200ms", "1s", "1m30s", "2h".
_DURATION_PART = _re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h)")

_DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


def duration_to_seconds(value: str) -> float | None:
    """``"6m0s"`` -> 360.0, ``"200ms"`` -> 0.2. ``None`` if not a duration.

    A reset given as a duration is an offset from NOW; one given as an epoch
    is an instant. Reading the first as the second puts the reset in 1970 and
    the caller never waits at all.

    Summed part by part rather than matched as a whole, so the order of the
    parts does not matter and a unit nobody anticipated is ignored rather than
    failing the string around it. Returns ``None`` on no recognised part, so
    that a bare epoch is not mistaken for zero seconds.
    """
    total = 0.0
    for match in _DURATION_PART.finditer(str(value)):
        total += float(match.group(1)) * _DURATION_UNITS[match.group(2)]
    return total if total > 0 else None


#: Kept as the private name this module used before the merge.
_duration_to_seconds = duration_to_seconds


def parse_headers(headers: Mapping[str, str]) -> RateLimitWindow:
    """Parse an HTTP response's headers into a :class:`RateLimitWindow`.

    The ONE parser for this vocabulary. Tolerant of missing or malformed
    values; surface as ``None`` rather than raise — a caller cannot crash on a
    vendor's bad header, and "we could not tell" has to stay distinguishable
    from "the budget is zero".
    """
    limit = _to_int(_first(headers, LIMIT_HEADERS))
    remaining = _to_int(_first(headers, REMAINING_HEADERS))

    reset_at: datetime | None = None
    reset_raw = _first(headers, RESET_HEADERS)
    reset_int = _to_int(reset_raw)
    if reset_int is not None:
        try:
            reset_at = datetime.fromtimestamp(reset_int, UTC)
        except (OverflowError, OSError, ValueError):
            reset_at = None
    elif reset_raw is not None:
        # Some vendors express the reset as a DURATION ("6m0s", "1s") rather
        # than an epoch. Read as an offset from now, which is what it means.
        seconds = _duration_to_seconds(reset_raw)
        if seconds is not None:
            reset_at = datetime.now(UTC) + timedelta(seconds=seconds)

    retry_after_s: int | None = None
    retry_raw = _ci_get(headers, "Retry-After")
    if retry_raw is not None:
        ra_int = _to_int(retry_raw)
        if ra_int is not None:
            retry_after_s = ra_int
        else:
            # RFC 7231 §7.1.3 — Retry-After MAY be an HTTP-date
            try:
                parsed = email.utils.parsedate_to_datetime(retry_raw)
                if parsed is not None:
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=UTC)
                    # Surface as a reset_at so the sleep function can
                    # compute the wait; leave retry_after_s as None to
                    # signal "use reset_at instead".
                    if reset_at is None:
                        reset_at = parsed
            except (TypeError, ValueError):
                pass

    return RateLimitWindow(
        limit=limit,
        remaining=remaining,
        reset_at=reset_at,
        retry_after_s=retry_after_s,
    )


# ---------------------------------------------------------------------------
# Sleep selection
# ---------------------------------------------------------------------------


def sleep_for_retry(
    window: RateLimitWindow,
    *,
    default_backoff_s: int = 5,
    max_backoff_s: int = 300,
    sleeper: Callable[[float], None] = _time.sleep,
) -> None:
    """Sleep the right amount based on the window's signal.

    Priority order:

    1. ``Retry-After`` (server's explicit directive).
    2. ``reset_at`` (computed from ``X-RateLimit-Reset`` or an
       HTTP-date ``Retry-After``).
    3. ``default_backoff_s`` (caller's fallback).

    Clamped to ``[0, max_backoff_s]``. The ``sleeper`` callable is
    injected so tests can capture the duration without actually
    sleeping.
    """
    seconds: float
    if window.retry_after_s is not None:
        seconds = float(window.retry_after_s)
    elif window.reset_at is not None:
        delta = (window.reset_at - datetime.now(UTC)).total_seconds()
        seconds = float(delta)
    else:
        seconds = float(default_backoff_s)

    seconds = max(0.0, min(seconds, float(max_backoff_s)))
    sleeper(seconds)


class RateLimited(Exception):
    """Raised by a connector when a request returns 429 (or equivalent).

    Carries the :class:`RateLimitWindow` parsed from the response so the
    caller can ``sleep_for_retry(exc.window)`` and resume. Typed so a
    PLINTH skill can recognize the signature without string-matching a
    generic ``RuntimeError``.
    """

    def __init__(self, window: RateLimitWindow, *, message: str | None = None) -> None:
        self.window = window
        super().__init__(message or self._format(window))

    @staticmethod
    def _format(w: RateLimitWindow) -> str:
        if w.retry_after_s is not None:
            return f"rate-limited: retry after {w.retry_after_s}s"
        if w.reset_at is not None:
            return f"rate-limited: reset at {w.reset_at.isoformat()}"
        return "rate-limited"


__all__ = [
    "RateLimitWindow",
    "RateLimited",
    "parse_headers",
    "sleep_for_retry",
]


# ---------------------------------------------------------------------------
# The other direction: admission control, for a service being called
# ---------------------------------------------------------------------------
#
# Everything above reads somebody else's limit. This writes ours.
#
# It lives in the same module deliberately. A limiter that publishes headers a
# different module parses is two halves of one protocol that can drift, and the
# whole reason this file exists is that there were three parsers of that same
# vocabulary. Emitter and parser share the header names below, so what we
# publish is by construction what we understand.


#: The canonical name for each field we publish, taken from the head of the
#: alias lists the parser already reads.
CANONICAL_LIMIT_HEADER = LIMIT_HEADERS[0]
CANONICAL_REMAINING_HEADER = REMAINING_HEADERS[0]
CANONICAL_RESET_HEADER = RESET_HEADERS[0]
RETRY_AFTER_HEADER = "Retry-After"


@dataclass
class TokenBucket:
    """Admission control for one caller, as a token bucket.

    A bucket rather than a fixed window because a window lets a caller spend
    its whole budget in the last instant of one window and the first of the
    next, which is the burst the limit exists to prevent.

    ``capacity`` is the burst a caller may take at once; ``refill_per_second``
    is the sustained rate. A caller that stays under the rate never notices
    this exists.

    Not thread-safe on its own: hold one lock per registry, which is what
    :class:`BucketRegistry` does.
    """

    capacity: float
    refill_per_second: float
    _tokens: float = field(default=0.0, init=False)
    _last: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        self.capacity = max(1.0, float(self.capacity))
        self.refill_per_second = max(1e-9, float(self.refill_per_second))
        self._tokens = self.capacity

    def take(self, now: float, cost: float = 1.0) -> bool:
        """Spend *cost* tokens if the bucket has them. ``True`` if admitted."""
        if self._last == 0.0:
            self._last = now
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_per_second)
        if self._tokens >= cost:
            self._tokens -= cost
            return True
        return False

    def remaining(self) -> int:
        """Whole tokens left. Floored, because half a request is not a request."""
        return int(self._tokens)

    def seconds_until(self, cost: float = 1.0) -> float:
        """How long until *cost* tokens exist. Zero when they already do."""
        if self._tokens >= cost:
            return 0.0
        return (cost - self._tokens) / self.refill_per_second


class BucketRegistry:
    """One bucket per caller, created on first sight.

    Keyed by whatever the caller is identified as — for an ingest face that is
    the site the credential resolved to, which means a partner cannot spend
    another partner's budget by claiming to be them: the key comes from the
    credential, never from the body.
    """

    def __init__(
        self,
        *,
        capacity: float,
        refill_per_second: float,
        clock: Callable[[], float] = _time.monotonic,
    ) -> None:
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self._clock = clock
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = _threading.Lock()

    def check(self, key: str, cost: float = 1.0) -> tuple[bool, dict[str, str]]:
        """``(admitted, headers)`` for one request from *key*.

        The headers are published on EVERY answer, not only a refusal. A caller
        that only learns its budget when it is rejected has already been
        rejected once, and ours pace on the published number.
        """
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = TokenBucket(self.capacity, self.refill_per_second)
                self._buckets[key] = bucket
            now = self._clock()
            admitted = bucket.take(now, cost)
            wait = bucket.seconds_until(cost)
            headers = {
                CANONICAL_LIMIT_HEADER: str(int(self.capacity)),
                CANONICAL_REMAINING_HEADER: str(bucket.remaining()),
                # An epoch, which is what the parser reads this name as. A
                # duration under this name would be read as a 1970 timestamp
                # and the caller would never wait.
                CANONICAL_RESET_HEADER: str(
                    int(_time.time() + max(wait, bucket.seconds_until(self.capacity)))
                ),
            }
            if not admitted:
                # Ceil, because a Retry-After of 0 invites an immediate retry
                # and is how a limiter becomes a hot loop.
                headers[RETRY_AFTER_HEADER] = str(max(1, _math.ceil(wait)))
            return admitted, headers

    def forget(self, key: str) -> None:
        """Drop a caller's bucket. For tests and for a caller going away."""
        with self._lock:
            self._buckets.pop(key, None)


def retry_after_seconds(value: str | None, *, cap: float | None = None) -> float | None:
    """One ``Retry-After`` header value, in seconds. ``None`` when unusable.

    The value-level door onto :func:`parse_headers`, for a caller that already
    has the one header in hand. Three notification channels each had their own
    copy of this, all seconds-only, so none of them understood the HTTP-date
    form the RFC allows and a vendor that sent one got the caller's default
    backoff instead of the wait it asked for.

    ``None`` rather than zero on an unusable value: a zero wait invites an
    immediate retry, which is how honouring a limit turns into ignoring one.
    """
    if value is None:
        return None
    window = parse_headers({"Retry-After": value})
    seconds: float | None = None
    if window.retry_after_s is not None:
        seconds = float(window.retry_after_s)
    elif window.reset_at is not None:
        seconds = (window.reset_at - datetime.now(UTC)).total_seconds()
    if seconds is None or seconds <= 0:
        return None
    return min(seconds, cap) if cap is not None else seconds
