# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A chart, drawn for a terminal.

``chart_spec`` settled what a chart IS and said, correctly, that "a kind
nothing renders would be a promise". Nothing in this tree rendered one —
the document was written to disk and a web component drew it somewhere
else — so every kind was a promise, including the one that shipped.

This is the smallest honest renderer: enough that a kind can be looked at
where it was made, and enough that registering a kind means something.

**Every series is drawn on its own scale, labelled with its own range.**
That is not a compromise, it is the correct reading of a chart holding kW
and degC: there is no shared axis, and one would be a lie about
comparability. What a reader compares here is SHAPE, and the numbers are
printed beside it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .numeric_format import format_number
from .units import annotate, declared

#: Eight levels is what a single character row can honestly carry.
_BLOCKS = "▁▂▃▄▅▆▇█"

#: Drawn where a bucket held no reading. Not a gap in the line — a gap in
#: the data, which is a different fact and the one worth seeing.
_EMPTY = "·"

DEFAULT_WIDTH = 60


def _instant(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def sparkline(values: list[float | None], *, width: int = DEFAULT_WIDTH) -> str:
    """*values* as one row of blocks, bucketed to *width*.

    A bucket with no reading is a dot rather than a baseline block: a flat
    line and missing data look identical otherwise, and they are the two
    readings a person most needs to tell apart.
    """
    if not values:
        return ""
    buckets: list[list[float]] = [[] for _ in range(max(1, min(width, len(values))))]
    for i, value in enumerate(values):
        if value is None:
            continue
        buckets[min(i * len(buckets) // len(values), len(buckets) - 1)].append(value)

    means = [sum(b) / len(b) if b else None for b in buckets]
    present = [m for m in means if m is not None]
    if not present:
        return _EMPTY * len(means)
    low, high = min(present), max(present)
    span = high - low
    out = []
    for mean in means:
        if mean is None:
            out.append(_EMPTY)
        elif span == 0:
            # Flat is mid-height, not floor: a constant drawn on the
            # baseline reads as zero.
            out.append(_BLOCKS[len(_BLOCKS) // 2])
        else:
            out.append(_BLOCKS[min(int((mean - low) / span * len(_BLOCKS)), len(_BLOCKS) - 1)])
    return "".join(out)


def _series(rows, *, time_column, value_column, series_column, key=None):
    """``{series: [value or None, ...]}`` ordered by instant."""
    ordered = sorted(
        (r for r in rows if _instant(r.get(time_column)) is not None),
        key=lambda r: _instant(r.get(time_column)),
    )
    out: dict[str, list[float | None]] = {}
    for row in ordered:
        if key is not None and not key(row):
            continue
        name = str(row.get(series_column) or value_column) if series_column else value_column
        value = row.get(value_column)
        out.setdefault(name, []).append(
            float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
            else None
        )
    return out


def _range_label(values: list[float | None], unit: str = "") -> str:
    """The range, always carrying its unit or saying there is none.

    ``f" {unit}" if unit else ""`` printed ``0 … 1170000`` for a channel
    nobody had declared a unit for, which reads as a count. It is watts.
    """
    present = [v for v in values if v is not None]
    if not present:
        return "no readings"
    low, high = min(present), max(present)
    if low == high:
        return annotate(f"flat at {format_number(low)}", unit)
    return annotate(f"{format_number(low)} … {format_number(high)}", unit)


def render_timeseries(rows, *, time_column, value_column, series_column,
                      unit_column="unit", width=DEFAULT_WIDTH) -> list[str]:
    """One labelled row per series, each on its own scale."""
    series = _series(rows, time_column=time_column, value_column=value_column,
                     series_column=series_column)
    if not series:
        return ["  no readings to draw"]
    units = {}
    for row in rows:
        name = str(row.get(series_column) or value_column) if series_column else value_column
        if declared(row.get(unit_column)):
            units[name] = str(row[unit_column]).strip()
    label_width = max(len(n) for n in series)
    out = []
    for name, values in series.items():
        out.append(
            f"  {name:<{label_width}}  {sparkline(values, width=width)}  "
            f"{_range_label(values, units.get(name, ''))}"
        )
    return out


def render_comparison(rows, *, time_column, value_column, series_column,
                      class_column="source_class", unit_column="unit",
                      width=DEFAULT_WIDTH) -> list[str]:
    """Measured against modelled, per channel, with the worst divergence.

    The divergence is the answer somebody opened this chart for. Two lines
    that look alike at terminal resolution can still be thirty degrees
    apart, so the number is printed rather than left to the eye.
    """
    from .chart_choice import MODELLED

    def measured(row):
        return str(row.get(class_column) or "").strip().casefold() not in MODELLED

    def modelled(row):
        return str(row.get(class_column) or "").strip().casefold() in MODELLED

    real = _series(rows, time_column=time_column, value_column=value_column,
                   series_column=series_column, key=measured)
    model = _series(rows, time_column=time_column, value_column=value_column,
                    series_column=series_column, key=modelled)
    # Only DECLARED units go in. An absent one is not stored as "" and then
    # skipped at render time; it is absent, and `_range_label` says so.
    units = {str(r.get(series_column) or value_column): str(r.get(unit_column)).strip()
             for r in rows if declared(r.get(unit_column))}

    out: list[str] = []
    for name in sorted(set(real) | set(model)):
        unit = units.get(name, "")
        out.append(f"  {name}")
        if name in real:
            out.append(f"    measured   {sparkline(real[name], width=width)}  "
                       f"{_range_label(real[name], unit)}")
        if name in model:
            out.append(f"    modelled   {sparkline(model[name], width=width)}  "
                       f"{_range_label(model[name], unit)}")
        if name in real and name in model:
            pairs = [
                (a, b) for a, b in zip(real[name], model[name], strict=False)
                if a is not None and b is not None
            ]
            if pairs:
                worst = max(pairs, key=lambda p: abs(p[1] - p[0]))
                gap = worst[1] - worst[0]
                out.append(
                    f"    worst gap  {annotate(format_number(abs(gap)), unit)} "
                    f"({'model high' if gap > 0 else 'model low'}, "
                    f"measured {format_number(worst[0])})"
                )
        elif name in model:
            out.append("    no measurement to compare this model against")
    return out or ["  nothing to compare"]


def render_state(rows, *, time_column, state_column, width=DEFAULT_WIDTH) -> list[str]:
    """Spans, with how long each lasted.

    A state has spans rather than a slope, and the duration is the fact —
    "SCRAM for 2m 0s" is what a reader wants and a line chart cannot say.
    """
    ordered = sorted(
        ((stamp, str(r.get(state_column) or "").strip())
         for r in rows if (stamp := _instant(r.get(time_column))) is not None),
        key=lambda pair: pair[0],
    )
    if not ordered:
        return ["  no states to draw"]

    spans: list[tuple[str, datetime, datetime]] = []
    label, start = ordered[0][1], ordered[0][0]
    for stamp, value in ordered[1:]:
        if value != label:
            spans.append((label, start, stamp))
            label, start = value, stamp
    spans.append((label, start, ordered[-1][0]))

    total = (ordered[-1][0] - ordered[0][0]).total_seconds() or 1.0
    label_width = max(len(s[0]) for s in spans) if spans else 1
    out = []
    for name, begins, ends in spans:
        seconds = (ends - begins).total_seconds()
        cells = max(1, round(seconds / total * width))
        out.append(
            f"  {name:<{label_width}}  {'█' * cells}  {_duration(seconds)}"
            f"  from {begins.isoformat().replace('+00:00', 'Z')}"
        )
    return out


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {rest}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def render(offer: Any, rows: list[dict[str, Any]], *, width: int = DEFAULT_WIDTH) -> str:
    """Draw whatever *offer* chose, or say why there is nothing to draw."""
    if not getattr(offer, "kind", ""):
        return f"  {getattr(offer, 'reason', 'nothing to draw')}"
    common = {
        "time_column": offer.time_column,
        "value_column": offer.value_column,
        "series_column": offer.series_column,
        "width": width,
    }
    if offer.kind == "comparison":
        lines = render_comparison(rows, **common)
    elif offer.kind == "state":
        lines = render_state(
            rows, time_column=offer.time_column,
            state_column=offer.series_column or offer.value_column, width=width,
        )
    else:
        lines = render_timeseries(rows, **common)
    return "\n".join(lines)


__all__ = [
    "DEFAULT_WIDTH",
    "render",
    "render_comparison",
    "render_state",
    "render_timeseries",
    "sparkline",
]
