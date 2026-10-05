# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Time utilities — ISO parsing, relative time formatting, now() helpers.

Consolidates repeated datetime patterns across the codebase.

Usage:
    from axiom.infra.time_utils import parse_iso, time_ago, now_iso, now_utc

    dt = parse_iso("2025-03-01T12:00:00Z")     # handles Z suffix
    s = time_ago("2025-03-01T12:00:00Z")        # "3d ago"
    s = time_ago(some_datetime)                  # also accepts datetime
    ts = now_iso()                               # "2025-03-25T14:30:00+00:00"
    dt = now_utc()                               # timezone-aware datetime
"""

from __future__ import annotations

from datetime import UTC, datetime

from .text_utils import pluralize


def now_utc() -> datetime:
    """Current time as timezone-aware UTC datetime."""
    return datetime.now(UTC)


def now_iso() -> str:
    """Current time as ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp string, handling trailing 'Z'.

    Returns a timezone-aware datetime. If the input has no timezone,
    UTC is assumed.
    """
    value = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def time_ago(
    value: str | datetime,
    *,
    now: datetime | None = None,
    compact: bool = True,
) -> str:
    """Human-readable relative time string.

    Args:
        value: ISO timestamp string or datetime object.
        now: Reference time (default: current UTC time).
        compact: If True, use short form ("3d ago"). If False, use
                 long form ("3 days ago").

    Returns:
        Relative time string, or the date portion on parse failure.
    """
    try:
        if isinstance(value, str):
            dt = parse_iso(value)
        else:
            dt = value
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)

        ref = now or now_utc()
        delta = ref - dt
        secs = int(delta.total_seconds())

        if secs < 0:
            return "just now"
        if secs < 60:
            return "just now"

        minutes = secs // 60
        hours = secs // 3600
        days = secs // 86400

        if compact:
            if days > 365:
                return f"{days // 365}y ago"
            if days > 30:
                return f"{days // 30}mo ago"
            if days > 0:
                return f"{days}d ago"
            if hours > 0:
                return f"{hours}h ago"
            return f"{minutes}m ago"
        else:
            if days > 365:
                n = days // 365
                return f"{pluralize(n, 'year')} ago"
            if days > 30:
                n = days // 30
                return f"{pluralize(n, 'month')} ago"
            if days > 0:
                return f"{pluralize(days, 'day')} ago"
            if hours > 0:
                return f"{pluralize(hours, 'hour')} ago"
            return f"{pluralize(minutes, 'minute')} ago"

    except Exception:
        if isinstance(value, str):
            return value[:10]
        return "?"


__all__ = ["parse_iso", "time_ago", "now_iso", "now_utc"]
