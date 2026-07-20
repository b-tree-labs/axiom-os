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
import time as _time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

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


def parse_headers(headers: Mapping[str, str]) -> RateLimitWindow:
    """Parse an HTTP response's headers into a :class:`RateLimitWindow`.

    Tolerant of missing or malformed values; surface as ``None`` rather
    than raise — connectors can't crash on a vendor's bad header.
    """
    limit = _to_int(_ci_get(headers, "X-RateLimit-Limit"))
    remaining = _to_int(_ci_get(headers, "X-RateLimit-Remaining"))

    reset_at: datetime | None = None
    reset_raw = _ci_get(headers, "X-RateLimit-Reset")
    reset_int = _to_int(reset_raw)
    if reset_int is not None:
        try:
            reset_at = datetime.fromtimestamp(reset_int, UTC)
        except (OverflowError, OSError, ValueError):
            reset_at = None

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
