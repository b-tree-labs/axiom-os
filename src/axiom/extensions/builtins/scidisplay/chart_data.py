# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Rows into series, so every surface draws the same figure from the same rows.

The step between a query result and a figure is small and entirely mechanical,
and it was written out by hand in the one place that drew charts. A second
surface would have written it again, and the two would have disagreed within a
month about which rows are modelled, what happens to a row with no timestamp,
and whether a channel's unit comes from its first row or its last.

None of those are questions about acquisition or about the web. They are
questions about drawing, so they are answered here, once.

## What it decides

**An instant may be a timestamp, an ISO string, or an epoch in seconds.** A
bucketed query returns the last of those, and a marshaller that silently
dropped every row of such a source would show an empty figure over real data.

**A row with no readable instant, or no numeric value, is not a reading.** It
is dropped rather than drawn at zero or at the epoch. The count is returned, so
a caller can say how many rather than quietly showing fewer.

**A modelled row is one whose provenance says so** — predicted, estimated or
simulated. It becomes its own series, dashed and paired with the measurement it
models, which is what lets a figure show a drifting prediction.

**A channel's unit is the first one its rows declare, and a row that declares a
different one is counted.** Two units under one channel name is a conformance
fault rather than a drawing decision, and silently taking the last one seen
would hide it behind a figure that looks fine.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .chart_svg import Series

__all__ = ["Marshalled", "MODELLED_CLASSES", "series_from_rows"]

#: Provenance values that mean "this was not measured". Anything else is a
#: measurement, including an absent value: a row that does not say is a reading.
MODELLED_CLASSES = frozenset({"predicted", "estimated", "simulated", "modelled", "modeled"})


@dataclass(frozen=True)
class Marshalled:
    """The series, and what did not become one."""

    series: list[Series]
    #: Rows dropped for having no readable instant or no numeric value.
    unusable: int = 0
    #: Channels whose rows declared more than one unit. A conformance fault,
    #: surfaced rather than resolved: this cannot know which one is right.
    conflicting_units: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.series)


def _instant(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    # A number is an epoch in SECONDS, which is what `extract(epoch from ts)`
    # yields and what a bucketed query therefore hands back. Not guessed
    # between seconds and milliseconds by magnitude: that heuristic is wrong
    # once and then invisible, and a caller holding milliseconds knows it.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def series_from_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    time_column: str = "ts",
    value_column: str = "value",
    series_column: str = "channel",
    unit_column: str = "unit",
    provenance_column: str = "source_class",
) -> Marshalled:
    """These rows as drawable series.

    The column names are arguments because the row shape belongs to whoever
    queried, not to the renderer. Their defaults are the conformed shape, which
    is what most callers have.
    """
    measured: dict[str, list[tuple[datetime, float]]] = {}
    modelled: dict[str, list[tuple[datetime, float]]] = {}
    units: dict[str, str] = {}
    conflicting: list[str] = []
    unusable = 0

    for row in rows:
        stamp = _instant(row.get(time_column))
        value = row.get(value_column)
        if stamp is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            unusable += 1
            continue
        name = str(row.get(series_column) or "")
        provenance = str(row.get(provenance_column) or "").strip().lower()
        bucket = modelled if provenance in MODELLED_CLASSES else measured
        bucket.setdefault(name, []).append((stamp, float(value)))

        unit = row.get(unit_column)
        if unit:
            unit = str(unit)
            if name not in units:
                units[name] = unit
            elif units[name] != unit and name not in conflicting:
                conflicting.append(name)

    series = [
        Series(name, sorted(points), unit=units.get(name, ""))
        for name, points in sorted(measured.items())
    ]
    # A model is named after what it models and paired with it, so the renderer
    # can draw it in that channel's colour, dashed, with the divergence between
    # them shaded.
    series += [
        Series(name, sorted(points), unit=units.get(name, ""),
               modelled=True, against=name if name in measured else "")
        for name, points in sorted(modelled.items())
    ]
    return Marshalled(series=series, unusable=unusable,
                      conflicting_units=tuple(sorted(conflicting)))
