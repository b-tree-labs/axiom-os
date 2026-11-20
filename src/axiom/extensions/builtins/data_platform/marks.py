# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Data-quality MARKS: what a reader is told about a span they cannot read as drawn.

``company.QUALITY`` is ADR-132's CLOSED consumer contract — a verdict on one
reading — and it does not grow here. A mark is a different thing: what a
surface says about a value or a span when the line cannot be trusted as drawn.
A chart hatches it "—" in a neutral tone (never a status colour, DC-11), and a
chat answer names it in words ("held from 14:02 to 14:20 (reason)").

The vocabulary is closed for the same reason QUALITY is: a surface switching on
it must be able to enumerate it.

``held``     a reading exists and its value was withheld: a quality verdict
             nulled it (ADR-132 D3: a ``bad`` reading has ``value = NULL``,
             raw retained), or a producer flagged it held.
``gap``      no reading at all where one was expected.
``stale``    the producer repeated a value it had stopped refreshing.
``suspect``  a value is present and its verdict says not to trust it
             (``suspect`` or ``bad`` with the value still carried).
``refused``  the serving door refused the read, so there is nothing to draw.

``good`` and ``saturated`` carry NO mark. ``good`` needs none. ``saturated``
is a genuine reading at the instrument's bound: the value is what the device
reported, the quality cell already says "saturated", and hatching it would
tell a reader a real measurement is absent.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .company import QUALITY

#: The closed marks vocabulary. Never mixed into QUALITY.
MARKS = ("held", "gap", "stale", "suspect", "refused")

#: Flags a producer may set on a point to say its value was held back.
HELD_FLAGS = frozenset({"held", "withheld"})

#: What a mark says when the point carries no reason of its own.
DEFAULT_REASON = {
    "held": "value withheld by a quality verdict",
    "gap": "no reading",
    "stale": "the source stopped refreshing this value",
    "suspect": "the reading's quality verdict says not to trust it",
    "refused": "the read was refused",
}


def _flags(point: Mapping[str, Any]) -> set[str]:
    raw = point.get("flags")
    if isinstance(raw, str):
        return {raw.strip().lower()}
    if isinstance(raw, Sequence):
        return {str(f).strip().lower() for f in raw}
    return set()


def quality_of(point: Mapping[str, Any]) -> str | None:
    """The point's quality when it is one of the closed vocabulary, else None (never "good")."""
    q = point.get("quality")
    if isinstance(q, str) and q.strip().lower() in QUALITY:
        return q.strip().lower()
    return None


def mark_for(point: Mapping[str, Any]) -> str | None:
    """The mark one gold row or point earns, or None when it reads as drawn.

    Precedence, most specific first: refused, held, gap, stale, suspect. A held
    reading has no value either, and it must not read as a gap: the reading
    exists and somebody decided to withhold it, which is a different fact.
    """
    flags = _flags(point)
    if point.get("refused") or "refused" in flags:
        return "refused"
    quality = quality_of(point)
    value_absent = "value" in point and point.get("value") is None
    if point.get("held") or flags & HELD_FLAGS:
        return "held"
    if value_absent:
        # A verdict that nulled the value is a hold; no verdict at all is a gap.
        return "held" if quality in ("bad", "suspect", "stale") else "gap"
    if quality == "stale" or "stale" in flags:
        return "stale"
    if quality in ("suspect", "bad"):
        return "suspect"
    return None


def reason_for(point: Mapping[str, Any]) -> str | None:
    """The producer's own reason for a point's verdict, when it gave one."""
    for key in ("mark_reason", "quality_reason", "reason"):
        value = point.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def mark_spans(points: Sequence[Mapping[str, Any]], *, time_key: str = "t") -> list[dict[str, Any]]:
    """Contiguous runs of one mark as spans ``[{"mark","start","end","reason"}]``.

    ``start`` and ``end`` are the first and last marked point's timestamps (a
    single marked point is a span that starts and ends on itself). ``reason`` is
    the run's first producer reason, else the mark's default sentence. Points
    without the time key are skipped: a span needs somewhere to be drawn.
    """
    spans: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for point in points:
        if not isinstance(point, Mapping):
            continue
        t = point.get(time_key, point.get("ts"))
        if t is None:
            continue
        mark = mark_for(point)
        if mark is None:
            current = None
            continue
        if current is not None and current["mark"] == mark:
            current["end"] = t
            if current["reason"] is None:
                current["reason"] = reason_for(point)
            continue
        current = {"mark": mark, "start": t, "end": t, "reason": reason_for(point)}
        spans.append(current)
    for span in spans:
        if span["reason"] is None:
            span["reason"] = DEFAULT_REASON[span["mark"]]
    return spans


def bucket_marks(
    points: Sequence[Mapping[str, Any]],
    *,
    step_s: int,
    channel_key: str = "channel",
    time_key: str = "ts",
    min_missing: int = 3,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Marks for a bucketed series: held buckets, and runs of missing buckets.

    A bucket whose rows all had their value withheld carries ``held`` > 0 and
    ``n`` == 0; a run of at least ``min_missing`` absent buckets between two
    present ones is a ``gap``. Shorter runs are not marked: a channel slower
    than the bucket leaves single empty buckets by design, and hatching each one
    would bury the gaps that matter. Bounded by ``limit`` so a ragged channel
    cannot turn the payload into a list of marks.
    """
    from datetime import UTC, datetime, timedelta

    def _at(value: Any) -> datetime | None:
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None

    def _iso(dt: datetime) -> str:
        return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    out: list[dict[str, Any]] = []
    by_channel: dict[Any, list[Mapping[str, Any]]] = {}
    for p in points:
        if isinstance(p, Mapping):
            by_channel.setdefault(p.get(channel_key), []).append(p)
    step = timedelta(seconds=max(1, int(step_s)))
    for channel, pts in by_channel.items():
        prev: datetime | None = None
        for p in sorted(pts, key=lambda q: str(q.get(time_key))):
            at = _at(p.get(time_key))
            if at is None:
                continue
            if prev is not None and at - prev >= step * (min_missing + 1):
                out.append(
                    {
                        "channel": channel,
                        "mark": "gap",
                        "start": _iso(prev + step),
                        "end": _iso(at),
                        "reason": DEFAULT_REASON["gap"],
                    }
                )
            if int(p.get("held") or 0) > 0 and not int(p.get("n") or 0):
                out.append(
                    {
                        "channel": channel,
                        "mark": "held",
                        "start": _iso(at),
                        "end": _iso(at + step),
                        "reason": DEFAULT_REASON["held"],
                    }
                )
            prev = at
    # Adjacent held buckets are one held span.
    merged: list[dict[str, Any]] = []
    for span in out:
        last = merged[-1] if merged else None
        if (
            last is not None
            and last["mark"] == span["mark"] == "held"
            and last["channel"] == span["channel"]
            and last["end"] == span["start"]
        ):
            last["end"] = span["end"]
            continue
        merged.append(span)
    return merged[:limit]


def describe_span(span: Mapping[str, Any], *, label: str = "") -> str:
    """One span in words: ``"<label> held from 14:02 to 14:20 (reason)"``."""

    def _clock(value: Any) -> str:
        text = str(value or "")
        if "T" in text:
            text = text.split("T", 1)[1]
        return text[:5] if len(text) >= 5 and text[2:3] == ":" else str(value)

    start, end = _clock(span.get("start")), _clock(span.get("end"))
    when = f"at {start}" if start == end else f"from {start} to {end}"
    reason = span.get("reason")
    head = f"{label} " if label else ""
    return f"{head}{span.get('mark')} {when}" + (f" ({reason})" if reason else "")


__all__ = [
    "DEFAULT_REASON",
    "HELD_FLAGS",
    "MARKS",
    "bucket_marks",
    "describe_span",
    "mark_for",
    "mark_spans",
    "quality_of",
    "reason_for",
]
