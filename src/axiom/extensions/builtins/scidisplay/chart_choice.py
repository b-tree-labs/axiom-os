# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Whether a table is worth drawing, and as what.

A table of readings over time is a picture somebody is about to draw by
hand. The rows already say whether they can be: a time axis, a numeric
value, more than one instant, and something that separates one series from
another. Nothing about that reasoning is domain-specific, and it was living
inside one extension's ``chart.suggest`` — so every other extension with a
table had no way to offer a chart at all.

**It picks from what is REGISTERED, not from a catalogue of chart types.**
``chart_spec`` ships one kind on purpose: "a kind nothing renders would be
a promise." So this returns ``timeseries`` or it returns a reason, and when
a table wants a picture nobody can draw yet it says exactly that rather
than naming a kind that does not exist. Adding kinds is how the choice gets
richer; pretending to choose is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .chart_spec import DataShape
from .numeric_format import format_number
from .table_spec import ABSENT, columns_from

#: Below this there is no trend to see, only points.
MIN_INSTANTS = 2

#: More distinct labels than this and a column is an identity rather than a
#: state. A console has a handful of modes; a channel list has hundreds.
_MAX_STATES = 12

#: Class names that mean a row is a model's output rather than a reading.
#: A window holding both is the case worth charting most: a mislabelled or
#: drifting prediction is invisible until the two lines are on one picture.
MODELLED = frozenset({"predicted", "estimated", "forecast", "simulated"})


@dataclass(frozen=True)
class ChartOffer:
    """What could be drawn from these rows, or why nothing can be.

    ``kind`` is empty when nothing fits, and ``reason`` is filled either
    way — a refusal that does not say why sends somebody to reshape data
    that was already fine.
    """

    kind: str = ""
    reason: str = ""
    time_column: str = ""
    value_column: str = ""
    series_column: str = ""
    channels: tuple[str, ...] = ()
    instants: int = 0
    #: True when the rows hold both measurements and model output for the
    #: same window. The comparison is the point of drawing it.
    compares_model_to_measurement: bool = False
    #: "inferred" or "stipulated". A reader of a shared chart should know
    #: whether the platform chose the kind or a person did — the same
    #: reason a sorted page says it sorted a window.
    decided: str = "inferred"
    #: Registered kinds these rows could also draw, best first. Named so a
    #: recommendation can be argued with rather than only accepted.
    alternatives: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.kind)

    def to_document(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "reason": self.reason}
        if self.kind:
            out.update(
                {
                    "time_column": self.time_column,
                    "value_column": self.value_column,
                    "series_column": self.series_column,
                    "channels": list(self.channels),
                    "instants": self.instants,
                    "compares_model_to_measurement": self.compares_model_to_measurement,
                    "decided": self.decided,
                    "alternatives": list(self.alternatives),
                }
            )
        return out


def _as_instant(value: Any) -> datetime | None:
    """*value* as a moment, or None. Never raises on a stray cell."""
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text or text == ABSENT:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


#: Fewer readings than this, spread far enough apart, are islands rather
#: than a line. Both halves are needed: five readings a second apart are a
#: series, and five an hour apart are five facts.
SPARSE_POINTS = 5
SPARSE_GAP_SECONDS = 3600.0


def _too_sparse(instants: set) -> bool:
    """Whether these readings are too far apart to join with a line.

    A ratio cannot express this. With two points the gap between them IS
    the span, so any test of gap-against-span is true by construction and
    fires on every two-point series or none. What matters is absolute: how
    many readings, and how far apart.
    """
    if len(instants) >= SPARSE_POINTS:
        return False
    ordered = sorted(instants)
    if len(ordered) < 2:
        return False
    gaps = [
        (b - a).total_seconds()
        for a, b in zip(ordered, ordered[1:], strict=False)
    ]
    typical = sorted(gaps)[len(gaps) // 2]
    return typical > SPARSE_GAP_SECONDS


def _categorical_column(
    rows: list[dict[str, Any]],
    declared: list[str],
    *,
    exclude: set[str],
    series_column: str = "",
) -> str:
    """A column of labels that changes over time WITHIN a series, or "".

    "Changes over the window" is not enough, and getting that wrong made
    `unit` a state: across a table it holds W and degC, so it looked like
    a label that moves. Within one channel it never moves at all — it is
    an attribute of the series, not a state of it.

    A state is a label that changes while the thing it describes stays the
    same thing: STARTUP then STEADY then SCRAM, all of one console.
    """
    for name in declared:
        if name in exclude or not name:
            continue
        values = [
            str(row.get(name) or "").strip()
            for row in rows
            if str(row.get(name) or "").strip() not in ("", ABSENT)
        ]
        if not values or len(values) < len(rows) // 2:
            continue
        if any(_is_number(row.get(name)) for row in rows):
            continue
        distinct = len(set(values))
        # A state has FEW distinct values — STARTUP, STEADY, SCRAM. The cap
        # is absolute rather than a fraction of the rows, because a
        # nine-row sample of a three-state console is still a three-state
        # console and a fraction rejected it.
        if not 1 < distinct <= _MAX_STATES:
            continue
        # Vacuous when the candidate IS the series: grouping a column by
        # itself gives one value per group every time. A console log whose
        # only column is its mode is exactly that case, and the check
        # rejected the clearest state there is.
        if series_column and name != series_column:
            within: dict[str, set[str]] = {}
            for row in rows:
                key = str(row.get(series_column) or "")
                value = str(row.get(name) or "").strip()
                if value and value != ABSENT:
                    within.setdefault(key, set()).add(value)
            if not any(len(seen) > 1 for seen in within.values()):
                continue
        return name
    return ""


def _unmet(needs: Any, shape: Any) -> str:
    """Which requirement the rows fail, in a reader's terms."""
    if needs.time_axis and not shape.time_axis:
        return "nothing here says when"
    if shape.numeric_columns < needs.numeric_columns:
        return (
            f"it needs {needs.numeric_columns} numeric column(s) and these "
            f"rows have {shape.numeric_columns}"
        )
    if needs.categorical_series and not shape.categorical_series:
        return "no column of labels changing over time"
    if needs.both_provenances and not shape.both_provenances:
        return (
            "these rows are all one provenance, so there is nothing to "
            "compare a model against"
        )
    if needs.series and not shape.series:
        return "nothing separates one series from another"
    return "the rows are the wrong shape"


def _registered_kinds(registered: Any) -> list[Any]:
    """The kinds available to choose from.

    ``registered`` may be names (a caller asking "what if I only had
    these") or left to the live registry.
    """
    from .chart_spec import lookup_kind, registered_kinds

    names = registered_kinds() if registered is None else registered
    out = []
    for name in names:
        try:
            out.append(lookup_kind(name))
        except Exception:  # noqa: BLE001 - an unknown name is simply absent
            continue
    return out


def offer_for(
    rows: list[dict[str, Any]],
    *,
    columns: Any = (),
    registered: Any = None,
    stipulated: str = "",
    series_hint: str = "",
) -> ChartOffer:
    """What chart these rows support.

    Decided from the rows rather than from column NAMES: a column called
    ``ts`` that holds free text is not a time axis, and one called ``t0``
    that holds timestamps is. Names are a convention and conventions differ
    per site; the values are the same everywhere.

    ``registered`` names the kinds that can actually be drawn, defaulting
    to what ``chart_spec`` has. Passing it lets a caller ask "what could I
    offer if I also had these" without this module inventing them.
    """
    if not rows:
        return ChartOffer(reason="no rows — nothing to draw")

    declared = [c.id for c in columns_from(columns)] if columns else list(rows[0])

    # A time axis: the column whose values actually parse as moments, and
    # most often. A table can carry two date-ish columns and only one of
    # them is the axis.
    best_time, best_hits = "", 0
    for name in declared:
        hits = sum(1 for row in rows if _as_instant(row.get(name)) is not None)
        if hits > best_hits:
            best_time, best_hits = name, hits
    if not best_time or best_hits < len(rows) // 2:
        return ChartOffer(
            reason="no time axis in these rows — nothing here says when, so "
            "there is no trend to draw"
        )

    instants = {
        stamp
        for row in rows
        if (stamp := _as_instant(row.get(best_time))) is not None
    }
    if len(instants) < MIN_INSTANTS:
        return ChartOffer(
            reason=f"every reading carries the same instant, so there is no "
            f"window to plot over — {len(instants)} moment is not a series"
        )

    value_column = next(
        (
            name
            for name in declared
            if name != best_time
            and sum(1 for row in rows if _is_number(row.get(name))) > len(rows) // 2
        ),
        "",
    )
    # No numeric column is not the end. A categorical channel over time is
    # a state — STARTUP, STEADY, SCRAM — and drawing one is exactly what
    # `silver.run_segments` exists for. Calling it undrawable was the
    # chooser telling a partner their console log was not a picture.
    # The series column is excluded: `channel` is what separates the lines,
    # not a state. Reading it as one turned an ordinary reading table into
    # a state chart, which is the chooser being clever about the wrong
    # column.


    # What separates one line from another. A column that groups and is not
    # the axis or the value: the channel, in most tables that have one.
    # A caller that knows which column names its series says so. Filtered
    # to one channel every candidate column holds one distinct value, and
    # the tie-break is arbitrary — a chart of one thermocouple came out
    # labelled with the stream name. Which column that is, is the caller's
    # domain knowledge and does not belong in here as a blessed name.
    series_column, channels = "", ()
    if series_hint and series_hint in declared:
        seen = {
            str(row.get(series_hint) or "").strip()
            for row in rows
            if str(row.get(series_hint) or "").strip() not in ("", ABSENT)
        }
        if seen:
            series_column, channels = series_hint, tuple(sorted(seen))

    for name in [] if series_column else declared:
        # `source_class` is never the series: it is what SPLITS a series
        # into measured and modelled, which the comparison kind consumes.
        # Left in, it won on distinct count and a comparison of one
        # thermocouple came out labelled "measured" and "predicted" with
        # the channel nowhere on the picture.
        if name in (best_time, value_column, "source_class"):
            continue
        seen = {
            str(row.get(name) or "").strip()
            for row in rows
            if str(row.get(name) or "").strip() not in ("", ABSENT)
        }
        # The MOST distinguishing column that still groups. A filter wants
        # the column with fewest values; a chart wants the opposite — a
        # column holding one value draws everything as a single line and
        # hides the very comparison somebody opened the chart for.
        if 1 <= len(seen) < len(instants) and len(seen) > len(channels):
            series_column, channels = name, tuple(sorted(seen))

    # The series column is excluded only when there IS a numeric column to
    # separate. With readings, `channel` is what separates the lines and
    # reading it as a state turned an ordinary table into a state chart.
    # Without readings — a console log of STARTUP/STEADY/SCRAM — the column
    # that groups and the state ARE the same column, and excluding it left
    # the chooser saying a console log is not a picture.
    #
    # `source_class` is never a state: it is provenance, and the comparison
    # kind already consumes it.
    exclude = {best_time, "source_class"}
    if value_column:
        exclude |= {value_column, series_column}
    categorical = _categorical_column(
        rows, declared, exclude=exclude, series_column=series_column
    )

    classes = {
        str(row.get("source_class") or "").strip().casefold()
        for row in rows
        if str(row.get("source_class") or "").strip()
    }
    compares = bool(classes & MODELLED) and bool(classes - MODELLED)

    shape = DataShape(
        time_axis=True,
        numeric_columns=1 if value_column else 0,
        series=bool(series_column),
        categorical_series=bool(categorical),
        both_provenances=compares,
    )

    # Score what is REGISTERED rather than run an if-ladder. A kind becomes
    # choosable by being registered, and the ones that fit but did not win
    # are named, so a recommendation can be argued with.
    fitting = [
        kind
        for kind in _registered_kinds(registered)
        if kind.needs is not None and kind.needs.satisfied_by(shape)
    ]
    if not fitting:
        if not value_column and not categorical:
            return ChartOffer(
                reason="no numeric column and nothing categorical to band — "
                "a table of free text is a table, not a chart"
            )
        return ChartOffer(
            reason="these rows trend over time, and no registered kind can "
            "draw this shape"
        )

    # Most demanding first: a kind that fits a narrower set of rows is the
    # more informative picture when it fits at all.
    fitting.sort(key=lambda k: k.needs.specificity, reverse=True)
    chosen = fitting[0]
    decided = "inferred"

    if stipulated:
        # A person asking for a kind outranks the inference, the way an
        # explicit `--site` outranks the binding. But a kind these rows
        # cannot draw is refused rather than drawn wrong: silently falling
        # back to the inferred kind would hand somebody a picture they did
        # not ask for and no sign that they did not get one.
        wanted = next((k for k in _registered_kinds(registered)
                       if k.name == stipulated), None)
        if wanted is None:
            names = ", ".join(sorted(k.name for k in _registered_kinds(registered)))
            return ChartOffer(
                reason=f"no chart kind {stipulated!r} is registered. There is: {names}"
            )
        if wanted.needs is not None and not wanted.needs.satisfied_by(shape):
            missing = _unmet(wanted.needs, shape)
            return ChartOffer(
                reason=f"these rows cannot draw a {stipulated!r}: {missing}"
            )
        chosen, decided = wanted, "stipulated"

    if chosen.name == "comparison":
        reason = (
            "these rows hold both measurements and model output over the "
            "same window — drawing them together is the only way a drifting "
            "prediction becomes visible"
        )
    elif chosen.name == "state":
        reason = (
            f"{categorical} is categorical over time — a state has spans, "
            "not a slope, and averaging one is meaningless"
        )
    else:
        # No instant count in the wording. It is the count in the rows
        # HANDED IN, and a caller that probed two hundred rows of a stream
        # of six thousand would be stating a fact about its own sample as
        # though it were about the data. The number stays in the document,
        # where a caller that knows what it fetched can use it.
        count = len(channels) or 1
        values = [
            row.get(value_column)
            for row in rows
            if _is_number(row.get(value_column))
        ]
        if values and min(values) == max(values):
            # "Trending" about a constant is a recommendation that
            # misdescribes the data, which is worse than no recommendation.
            # Still worth drawing: a flat line is a real answer to "did
            # anything happen", and often the one somebody needed.
            reason = (
                f"{count} series, and every reading is "
                f"{format_number(values[0])} — flat, not trending"
            )
        elif _too_sparse(instants):
            # A line between two distant points asserts a continuity nobody
            # measured — the same objection this codebase already makes to
            # interpolating a step-held channel.
            reason = (
                f"{len(instants)} readings spread over the window — far "
                f"enough apart that a line between them would invent the "
                f"values in between"
            )
        else:
            reason = (
                f"{count} series trending over time"
                if count > 1
                else "these readings trend over time"
            )

    if categorical and chosen.name != "state":
        series_column = series_column or categorical

    if decided == "stipulated":
        reason = f"{chosen.name} was asked for — {chosen.summary}"

    return ChartOffer(
        kind=chosen.name,
        reason=reason,
        decided=decided,
        alternatives=tuple(k.name for k in fitting if k.name != chosen.name),
        time_column=best_time,
        value_column=value_column,
        series_column=series_column,
        channels=channels,
        instants=len(instants),
        compares_model_to_measurement=compares,
    )


__all__ = ["MIN_INSTANTS", "MODELLED", "ChartOffer", "offer_for"]
