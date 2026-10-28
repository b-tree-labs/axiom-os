# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The everyday vocabulary of ``axi data``: windows, buckets, suggestions, next steps.

Nothing here opens a connection or imports a skill, so every rule is testable
without a database. The skills in :mod:`.skills.friendly` compose the served
verbs; this module holds the parts of that composition that are just language:

- one window vocabulary (``--start/--end`` = ``--from/--to``, ``--day``,
  ``--last 7d``, ``--yesterday``) resolved to one ``(start, end)`` pair;
- a bucket chosen from the window's length when the caller did not say;
- "did you mean" suggestions against what the catalogue really holds;
- the ``next:`` block, built only from values that appear in the result.

A window is always UTC and half-open (``start`` inclusive, ``end`` exclusive),
the same convention the served verbs use.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

# ---------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^\s*(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|"
                          r"h|hr|hrs|hour|hours|d|day|days|w|wk|wks|week|weeks)\s*$", re.I)

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
_UNIT_WORD = {"s": "second", "m": "minute", "h": "hour", "d": "day", "w": "week"}


class WindowError(ValueError):
    """A window the caller asked for that cannot mean anything."""


def _unit_key(unit: str) -> str:
    return unit.strip().lower()[0]


def parse_duration(text: str) -> timedelta:
    """``7d``, ``36h``, ``90m``, ``2 weeks`` as a timedelta."""
    m = _DURATION_RE.match(str(text or ""))
    if m is None or int(m.group(1)) == 0:
        raise WindowError(
            f"cannot read {text!r} as a duration; try 90m, 6h, 7d or 2w"
        )
    return timedelta(seconds=int(m.group(1)) * _UNIT_SECONDS[_unit_key(m.group(2))])


def interval_words(text: str) -> str:
    """``1h`` or ``15 minutes`` as the interval grammar the served verbs take: ``1 hour``."""
    m = _DURATION_RE.match(str(text or ""))
    if m is None or int(m.group(1)) == 0:
        raise WindowError(
            f"cannot read {text!r} as a bucket size; try 15m, 1h or 1d "
            "(or words, like '15 minutes')"
        )
    n = int(m.group(1))
    word = _UNIT_WORD[_unit_key(m.group(2))]
    return f"{n} {word}{'' if n == 1 else 's'}"


def interval_seconds(text: str) -> int:
    m = _DURATION_RE.match(str(text or ""))
    if m is None:
        raise WindowError(f"cannot read {text!r} as a bucket size")
    return int(m.group(1)) * _UNIT_SECONDS[_unit_key(m.group(2))]


def parse_instant(value: Any) -> datetime | None:
    """A UTC-aware datetime from what a verb hands back (datetime or ISO text), or None."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace(" ", "T")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0").rstrip(".") + "Z"


def day_bounds(day: str) -> tuple[datetime, datetime]:
    try:
        d = date.fromisoformat(str(day).strip())
    except ValueError:
        raise WindowError(f"cannot read {day!r} as a day; use YYYY-MM-DD") from None
    start = datetime(d.year, d.month, d.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


@dataclass(frozen=True)
class Window:
    start: str | None
    end: str | None
    label: str


def resolve_window(
    *,
    start: str | None = None,
    end: str | None = None,
    day: str | None = None,
    last: str | None = None,
    yesterday: bool = False,
    now: datetime | None = None,
    normalize: bool = True,
) -> Window | None:
    """One ``(start, end)`` from the flags a caller used, or None when they used none.

    ``--start/--end`` and ``--from/--to`` are the same flags (the parser aliases
    them), so this sees one pair. ``--day`` and ``--yesterday`` are a whole UTC
    day; ``--last 7d`` is relative to now, because "last" means the last of
    something that is still happening. The default for a verb that wants one
    (``show``) is not "now": it is the latest day with data, which only the
    catalogue knows, so it is decided in the skill.
    """
    chosen = [name for name, used in (
        ("--start/--end", bool(start or end)),
        ("--day", bool(day)),
        ("--last", bool(last)),
        ("--yesterday", bool(yesterday)),
    ) if used]
    if len(chosen) > 1:
        raise WindowError(
            f"{' and '.join(chosen)} each pick a window; use one of them"
        )
    now = (now or datetime.now(UTC)).replace(microsecond=0)
    if day:
        s, e = day_bounds(day)
        return Window(iso(s), iso(e), f"{s.date().isoformat()} (UTC)")
    if yesterday:
        s, e = day_bounds((now.date() - timedelta(days=1)).isoformat())
        return Window(iso(s), iso(e), f"{s.date().isoformat()} (UTC)")
    if last:
        span = parse_duration(last)
        return Window(iso(now - span), iso(now), f"the last {last.strip()}")
    if start or end:
        if normalize:
            s, e = _instant(start, "--start"), _instant(end, "--end")
        else:
            s, e = start or None, end or None
        return Window(s, e, f"{s or '...'} to {e or '...'}")
    return None


def _instant(text: str | None, flag: str) -> str | None:
    """A date or a time as the one UTC spelling the served verbs read; a date is its midnight."""
    if not text:
        return None
    dt = parse_instant(text)
    if dt is None:
        raise WindowError(f"cannot read {text!r} as a date or time for {flag}; use 2026-03-10 or 2026-03-10T14:30:00Z")
    return iso(dt)


_BUCKET_STEPS: tuple[tuple[int, str], ...] = (
    (3600, "1 minute"),
    (6 * 3600, "5 minutes"),
    (86400, "30 minutes"),
    (3 * 86400, "1 hour"),
    (14 * 86400, "6 hours"),
    (62 * 86400, "1 day"),
    (400 * 86400, "7 days"),
)


def choose_bucket(window_seconds: float) -> str:
    """A bucket that keeps a window to roughly 50-100 points, in the interval grammar."""
    for limit, bucket in _BUCKET_STEPS:
        if window_seconds <= limit:
            return bucket
    return "30 days"


def window_seconds(start: str | None, end: str | None) -> float | None:
    s, e = parse_instant(start), parse_instant(end)
    if s is None or e is None:
        return None
    return max((e - s).total_seconds(), 0.0)


# ---------------------------------------------------------------------------
# suggestions
# ---------------------------------------------------------------------------


def close_matches(name: str, candidates: Iterable[str], n: int = 3) -> list[str]:
    """The candidates nearest to ``name``: a case-insensitive hit first, then fuzzy ones."""
    pool = sorted({str(c) for c in candidates if c})
    lowered = {c.lower(): c for c in pool}
    out: list[str] = []
    exact = lowered.get(str(name).lower())
    if exact:
        return [exact]
    # Prefix/substring hits read as "did you mean" better than character-level ones
    # for short stems like `fluid_temp`.
    needle = str(name).lower()
    for c in pool:
        if c not in out and needle and (c.lower().startswith(needle) or needle in c.lower()):
            out.append(c)
    for c in difflib.get_close_matches(str(name), pool, n=n, cutoff=0.6):
        if c not in out:
            out.append(c)
    return out[:n]


def unknown_message(kind: str, name: str, where: str, candidates: Sequence[str], *, listing: str = "") -> str:
    """``no role 'fluid_temp' at example-loop; did you mean fluid_temperature?``"""
    at = f" at {where}" if where else ""
    guesses = close_matches(name, candidates)
    head = f"no {kind} {name!r}{at}"
    if guesses:
        return f"{head}; did you mean {' or '.join(guesses)}?"
    if listing:
        return f"{head}; {listing}"
    return head


# ---------------------------------------------------------------------------
# next steps
# ---------------------------------------------------------------------------


def cli_name() -> str:
    """The command this install is called (``axi``, or the product's own name)."""
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name or "axi"
    except Exception:  # noqa: BLE001 — a hint must never be the reason a read fails
        return "axi"


def next_block(lines: Sequence[str]) -> str:
    """The one ``next:`` block that ends a text answer, or empty when there is nothing to say."""
    lines = [ln for ln in lines if ln]
    if not lines:
        return ""
    return "\n\nnext:\n" + "\n".join(f"  {ln}" for ln in lines[:3])


def _site_flag(site: str | None) -> str:
    return f" --site {site}" if site else ""


def next_for_roles(roles: Sequence[dict[str, Any]], prog: str) -> list[str]:
    """Follow-ups for a roles listing: look at one, in a real role at a real site."""
    for entry in roles:
        sites = entry.get("sites") or []
        if entry.get("role") and sites:
            role, site = entry["role"], sites[0]
            return [
                f"{prog} data show {role} --site {site}",
                f"{prog} data channels --site {site} --role {role}",
                f"{prog} data latest --site {site} --role {role}",
            ]
    return []


def _runnable_channel(rows: Sequence[dict[str, Any]], stamp: str) -> dict[str, Any] | None:
    """The channel with the newest reading among those whose name alone is enough to ask for it."""
    feeds: dict[Any, set[Any]] = {}
    for r in rows:
        feeds.setdefault(r.get("channel"), set()).add(r.get("feed"))
    unique = [r for r in rows if r.get("channel") and len(feeds[r["channel"]]) == 1]
    return max(unique, key=lambda r: str(r.get(stamp) or ""), default=None)


def next_for_channels(rows: Sequence[dict[str, Any]], site: str, prog: str) -> list[str]:
    role = next((r["role"] for r in rows if r.get("role")), None)
    chan = _runnable_channel(rows, "last_ts")
    out: list[str] = []
    if role:
        out.append(f"{prog} data show {role} --site {site}")
        out.append(f"{prog} data latest --site {site} --role {role}")
    if chan:
        day = _day_of(chan.get("last_ts"))
        tail = f" --day {day}" if day else ""
        out.append(f"{prog} data show {chan['channel']} --site {site}{tail}")
    return out


def next_for_latest(rows: Sequence[dict[str, Any]], site: str, prog: str) -> list[str]:
    out: list[str] = []
    role = next((r["role"] for r in rows if r.get("role")), None)
    pick = _runnable_channel([r for r in rows if r.get("ts")], "ts")
    if role:
        out.append(f"{prog} data show {role} --site {site}")
    if pick:
        day = _day_of(pick.get("ts"))
        tail = f" --day {day}" if day else ""
        out.append(f"{prog} data show {pick['channel']} --site {site}{tail}")
    out.append(f"{prog} data channels --site {site}")
    return out


def next_for_show(data: dict[str, Any], prog: str) -> list[str]:
    site, target = data.get("site"), data.get("target")
    points = data.get("series") or []
    day = data.get("day") or (_day_of(points[-1].get("t")) if points else "")
    channels = data.get("channels") or []
    out: list[str] = []
    if data.get("kind") == "role" and channels:
        c = channels[0]
        tail = f" --day {day}" if day else ""
        out.append(f"{prog} data show {c}{_site_flag(site)}{tail}")
    if target:
        week = _week_ending(day)
        out.append(f"{prog} data show {target}{_site_flag(site)} {week} --every 1d")
    if data.get("kind") == "role" and target:
        out.append(f"{prog} data latest{_site_flag(site)} --role {target}")
    elif data.get("kind") == "channel" and target:
        out.append(f"{prog} data latest{_site_flag(site)} --channel {target}")
    return out


def _week_ending(day: str) -> str:
    """``--start D-6 --end D+1`` around a real day, else the relative ``--last 7d``."""
    try:
        d = date.fromisoformat(day)
    except (TypeError, ValueError):
        return "--last 7d"
    return f"--start {(d - timedelta(days=6)).isoformat()} --end {(d + timedelta(days=1)).isoformat()}"


def _day_of(value: Any) -> str:
    dt = parse_instant(value)
    return dt.date().isoformat() if dt else ""
