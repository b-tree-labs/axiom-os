# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Zooming, panning, and what a chart shows before anybody has zoomed.

## Zoom is a change to the document, not to the viewport

A surface could scale the picture it already has. That is the cheap
implementation and it is the wrong one, for three reasons that all show up the
first time somebody uses it.

It shows no more detail. A viewport transform over marks already bucketed at
one hour draws the same hourly marks larger; the reader zoomed in and learned
nothing, which is the definition of a control that does not work.

It cannot be shared. The window is what the figure is OF. Scaled in a browser
it lives in that browser, so the link somebody sends is the figure before they
looked at it.

And every surface would implement it separately, so a chart in a terminal, a
chart in a chat and a chart on a page would not agree about what "zoom in"
means.

So zoom takes a window and returns a window, the document carries it, and the
bucket is re-chosen every time — which is the whole point, because a narrower
window earns finer marks. A terminal passes a flag, a page turns a scroll
wheel, and both produce the same document and therefore the same figure.

A surface showing several charts holds ONE window and applies it to all of
them, which is why the useful functions here take a window rather than a spec.

## What it refuses to do

**It will not zoom out past the data.** Empty space either side of a series
looks like a gap in the readings.

**It will not zoom in below what the readings can resolve.** Twenty marks is
already a thin chart; a window narrower than twenty of the finest buckets is a
handful of dots claiming to be a series.

Both clamp rather than fail, and both SAY they clamped, because a control that
silently does nothing is one a person keeps pressing.

## The default window

Anchored to the last reading, never to the clock.

A window of "the last day" measured from now is empty the moment ingestion
stops, and an empty chart looks exactly like a working chart of a quiet
signal. Anchored to the data it always shows the most recent real operation,
and the time axis says when that was — so a feed that died on Tuesday is
visible as a figure ending on Tuesday, rather than as a blank.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

__all__ = [
    "ACTIVE_FRACTION",
    "LEAD_IN",
    "MIN_RUN",
    "QUIET_PERCENTILE",
    "RESTING_SPREAD_MULTIPLE",
    "RUN_SHARE",
    "active_span",
    "DEFAULT_SPAN",
    "MIN_MARKS",
    "TARGET_MARKS",
    "Bounds",
    "Drawable",
    "bucket_seconds",
    "cadence_of",
    "choose_bucket",
    "default_window",
    "drawable_bounds",
    "pan",
    "settle",
    "span_seconds",
    "window_bounds",
    "zoom",
]

#: How many marks a chart aims for. Chosen against pixels, not against rows:
#: past roughly this many, more marks is more ink and no more information, and
#: every one of them costs a renderer and a network.
TARGET_MARKS = 800

#: Below this a chart is a handful of dots claiming to be a series, so it is
#: the floor a zoom will not go under.
MIN_MARKS = 20

#: How much a scroll notch changes the span. A factor of two per notch gets
#: from a month to a minute in fifteen notches and never feels like nothing
#: happened.
ZOOM_STEP = 2.0

#: What a surface shows before anybody has asked for a window. A day contains
#: a day's operation and is a span people say out loud.
DEFAULT_SPAN = timedelta(days=1)

#: The durations the spec's own grammar accepts, smallest first. A bucket is
#: picked from this ladder rather than computed freely so the value is one a
#: person recognises — "5m" reads, "3.7m" does not, and a spec is meant to be
#: read as much as executed.
BUCKET_LADDER: tuple[tuple[str, float], ...] = (
    ("100ms", 0.1),
    ("500ms", 0.5),
    ("1s", 1),
    ("5s", 5),
    ("10s", 10),
    ("30s", 30),
    ("1m", 60),
    ("5m", 300),
    ("15m", 900),
    ("30m", 1800),
    ("1h", 3600),
    ("6h", 21600),
    ("12h", 43200),
    ("1d", 86400),
    ("7d", 604800),
    ("30d", 2592000),
)

_BUCKET = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m|h|d)$")
_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


class WindowError(ValueError):
    """A window cannot be read, or an operation on one makes no sense."""


def bucket_seconds(bucket: str) -> float:
    """The width of a spec bucket, in seconds."""
    match = _BUCKET.match(str(bucket))
    if not match:
        raise WindowError(f"not a bucket duration: {bucket!r}")
    return float(match.group(1)) * _UNIT_SECONDS[match.group(2)]


def choose_bucket(span: float, *, target: int = TARGET_MARKS) -> str:
    """The smallest ladder step that keeps the mark count near *target*.

    Bounding the top must not flatten the bottom: a ten-minute window has to
    stay legible, so this takes the smallest step that is big enough rather
    than the biggest step available.
    """
    if span <= 0:
        raise WindowError("a window with no span cannot be bucketed")
    ideal = span / max(target, 1)
    for name, seconds in BUCKET_LADDER:
        if seconds >= ideal:
            return name
    return BUCKET_LADDER[-1][0]


def instant(value: Any) -> datetime | None:
    """A timestamp from whatever a document happens to carry, or ``None``."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _stamp(moment: datetime) -> str:
    """The spelling a window field uses, so a round trip is byte-identical."""
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Bounds:
    """How far the readings themselves go. A zoom never leaves this."""

    earliest: datetime
    latest: datetime

    def __post_init__(self) -> None:
        if self.latest < self.earliest:
            raise WindowError("bounds end before they begin")

    @property
    def span(self) -> float:
        return (self.latest - self.earliest).total_seconds()


def _and(names) -> str:
    """``a``, ``a and b``, ``a, b and c`` — a list a person would say."""
    names = list(names)
    if len(names) <= 1:
        return names[0] if names else ""
    return f"{', '.join(names[:-1])} and {names[-1]}"


@dataclass(frozen=True)
class Drawable:
    """Where to open a figure over several series, and why there."""

    bounds: Bounds
    #: What the caller should say about the choice, in its own words.
    notes: tuple[str, ...] = ()
    #: True when no instant lies inside every series, so the window holds them
    #: all but shows no two of them together. Said rather than left for the
    #: reader to infer from an empty half of the plot.
    disjoint: bool = False


#: How far a value must depart from its own quiet level to count as activity,
#: as a fraction of the channel's observed range.
#:
#: Relative, not absolute, and that is the whole point: no threshold is declared,
#: nothing about the quantity is known, and the same rule works for a power
#: channel spanning six decades and a temperature spanning seven degrees.
#:
#: 0.001 measured against a real operating day. The console's power sits at
#: 0.000182 W overnight and peaks at 1.08 MW, so a thousandth of the range is
#: about 1.1 kW — and an earlier measurement showed the answer barely moves for
#: any threshold between 0.01 W and 100 kW, seven orders of magnitude. A
#: fraction anywhere in that band gives the same window, which is what makes a
#: relative constant safe here rather than a guess.
ACTIVE_FRACTION = 0.001

#: How many multiples of a channel's OWN resting spread a departure must clear.
#:
#: The fraction above is not enough by itself, and the reason is that it was
#: measured on a channel that rests at ~0 and peaks six decades higher, where a
#: thousandth of the range is an enormous multiple of the resting spread. On a
#: temperature resting between 15 and 18 on a day that peaks at 378.8, a
#: thousandth of the range is 0.38 — UNDER the resting wander — so every resting
#: sample read as a departure and the window became the whole calendar day,
#: which is the exact outcome this feature exists to avoid.
#:
#: So the threshold is the LARGER of the two: a departure has to be big against
#: the channel's range and big against how much the channel moves while resting.
#: The spread is measured as the median absolute deviation from the quiet level,
#: which a long excursion barely moves — the mean would be dragged by it.
#:
#: 4 measured across a factor of four: every multiple from 2 to 8 gives the same
#: window on all seven channels of a real operating day. A constant that only
#: works at one value is a fitted parameter; one that holds across a band is a
#: rule.
RESTING_SPREAD_MULTIPLE = 4.0

#: The shortest stretch that counts as activity on its own, as a fraction of the
#: record being examined.
#:
#: Without this, ONE sample sets the edge. Measured: a channel resting at
#: exactly 0 with real activity from 07:54 to 15:22 returned 01:14 to 23:30,
#: because a single one-minute sample of 1.0 sat near each end of the day. The
#: real stretches were 22 to 88 minutes and the strays were one sample — a
#: twenty-fold separation, where their magnitudes differ only five-fold, which
#: is why this is about duration first.
#:
#: 0.005 sits in the middle of the band that works: a stray is 0.0007 of the
#: record and the shortest real stretch is 0.019, so this is seven times above
#: one and four times below the other. Expressed as a fraction rather than a
#: count of samples so it means the same thing at every bucket.
MIN_RUN = 0.005

#: A stretch shorter than :data:`MIN_RUN` survives if its peak departure is at
#: least this share of the largest departure in the record.
#:
#: This is what keeps a pulse. A pulse is over in milliseconds, so at any bucket
#: a chart would probe with it is ONE sample — the same width as a stray, and
#: the most important thing in the record. A duration rule alone would discard
#: it. So: a stray is brief AND small; an event is brief OR large.
RUN_SHARE = 0.10

#: Which percentile is treated as the quiet level.
#:
#: The MEDIAN, because the quiet level is the signal's RESTING state and the
#: resting state is whatever it spends most of its time at.
#:
#: This was the tenth percentile first, and a test caught why that is wrong: a
#: low percentile assumes the resting state is the low one. A signal that DIPS
#: from a high resting level — 100 all day, 5 for seven hours — then has its dip
#: chosen as "quiet", so the twenty-three quiet hours read as activity and the
#: event reads as rest. Exactly inverted.
#:
#: Not the mean, which the active period drags toward itself; not the minimum,
#: which a single dropout at zero would define. The median is the resting level
#: whenever rest occupies most of the window, which is precisely when this
#: feature earns anything.
QUIET_PERCENTILE = 0.50

#: How much of the active span to show before it begins, as a fraction of that
#: span. A window that starts exactly at the first active reading clips the rise,
#: and the rise is the part a reader is looking for.
LEAD_IN = 0.05


def _percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    i = min(len(sorted_values) - 1, max(0, int(round(fraction * (len(sorted_values) - 1)))))
    return sorted_values[i]


def _runs(flags: list[bool]) -> list[tuple[int, int]]:
    """Inclusive index ranges of consecutive ``True``, nothing bridged.

    Bridging short false gaps was tried and is worse: on a channel whose
    resting level wanders, the gaps between resting departures are short too,
    so bridging welds the resting stretches onto the excursion and hands back
    the whole calendar day again.
    """
    out: list[tuple[int, int]] = []
    start: int | None = None
    for i, on in enumerate(flags):
        if on and start is None:
            start = i
        elif not on and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(flags) - 1))
    return out


def active_span(
    points: list[tuple[datetime, float]],
    *,
    fraction: float = ACTIVE_FRACTION,
    quiet_at: float = QUIET_PERCENTILE,
    lead_in: float = LEAD_IN,
    resting_multiple: float = RESTING_SPREAD_MULTIPLE,
    min_run: float = MIN_RUN,
    run_share: float = RUN_SHARE,
) -> tuple[datetime, datetime] | None:
    """When this signal was DOING something, ignoring long quiet stretches.

    Returns `(start, end)` or `None` when no stretch of the record stands out
    from the rest of it — in which case there is nothing to frame and the caller
    should show what it would have shown anyway, which is the whole record.

    ## Why departure from a quiet level rather than presence of readings

    Presence is the obvious measure and it is the wrong one. A console
    reports every minute for twenty-four hours whether or not anything is
    happening,
    so a gap-based rule finds one unbroken run and narrows nothing. What changes
    is the VALUE: 0.000182 W overnight, 1.08 MW at power. So activity is measured
    on what the signal says, not on whether it spoke.

    ## Why a fraction of the channel's own range

    Because nothing here knows what the quantity is. An absolute threshold would
    have to be declared per channel per site, and a threshold is exactly the kind
    of number that gets declared once and then quietly misapplied to the next
    instrument. A fraction of the observed range is self-calibrating: it means the
    same thing for watts, degrees and rod units.

    ## Departure, in either direction

    `abs(value - quiet)`, so a signal that DIPS from a high resting level is as
    active as one that rises from a low one. Keying on "above the quiet level"
    would have been simpler and would silently miss half the shapes a reader
    cares about.

    ## Why a fraction of the range is not enough on its own

    Because a resting level is not a constant. A temperature resting between 15
    and 18 on a day peaking at 378.8 wanders further at rest than a thousandth
    of its range, so every resting sample read as a departure and the window
    came back as the whole calendar day. The threshold therefore also has to
    clear the channel's own resting spread — see
    :data:`RESTING_SPREAD_MULTIPLE`.

    ## Why the edges are not simply the first and last departure

    Because one sample would set them. A channel resting at exactly 0 with real
    activity from 07:54 to 15:22 returned 01:14 to 23:30 on the strength of two
    single-minute samples of 1.0. So departures are grouped into consecutive
    runs and a run has to earn its place: long enough (:data:`MIN_RUN`) or big
    enough (:data:`RUN_SHARE`). A stray is brief and small; a pulse is brief and
    large, and it stays.

    Measured across all seven channels of one real operating day, this lands
    every one of them inside 07:54-15:25 against a declared power threshold's
    07:39-15:38 — knowing nothing about what any of them measures. Before it,
    four of the seven returned essentially the whole day.
    """
    usable = [(at, float(v)) for at, v in points if v is not None]
    if not usable:
        return None
    values = sorted(v for _, v in usable)
    lo, hi = values[0], values[-1]
    span = hi - lo
    if span <= 0:
        # A flat channel is never active. Returning None rather than the whole
        # record keeps "no activity" distinguishable from "active throughout".
        return None
    quiet = _percentile(values, quiet_at)
    # The spread the channel shows WHILE RESTING, as a median absolute
    # deviation — a long excursion barely moves it, where the mean would be
    # dragged toward the excursion and hide it.
    spread = _percentile(sorted(abs(v - quiet) for v in values), 0.50)
    threshold = max(span * fraction, spread * resting_multiple)

    ordered = sorted(usable, key=lambda pair: pair[0])
    flags = [abs(v - quiet) > threshold for _, v in ordered]
    runs = _runs(flags)
    if not runs:
        return None

    examined = (ordered[-1][0] - ordered[0][0]).total_seconds()
    peaks = [max(abs(ordered[i][1] - quiet) for i in range(a, b + 1)) for a, b in runs]
    biggest = max(peaks)
    kept = [
        (a, b)
        for (a, b), peak in zip(runs, peaks)
        if (ordered[b][0] - ordered[a][0]).total_seconds() >= min_run * examined
        or (biggest > 0 and peak >= run_share * biggest)
    ]
    if not kept:
        return None

    first, last = ordered[kept[0][0]][0], ordered[kept[-1][1]][0]
    reach = (last - first) * lead_in
    # Never earlier than the record itself: padding into a period with no
    # readings would frame emptiness, which is what this exists to avoid.
    earliest = ordered[0][0]
    return (max(earliest, first - reach), last)


def drawable_bounds(extents: Mapping[str, tuple[datetime, datetime]]) -> Drawable:
    """Bounds that hold ALL of these series, preferring where they overlap.

    A figure used to open on the union of its series: earliest first reading
    to latest last. Over one feed that is right. Over a comparison it is
    actively wrong — a measurement that stops in April against a model that
    runs to June opens on a window at the June end, where the measurement does
    not exist, and draws one line out of two. The reader asked to compare them
    and got a picture that could not.

    So: the OVERLAP, when there is one. That is the only span where the
    comparison the reader asked for can actually be drawn. Where the series
    never overlap, fall back to the union and say so, because "these were
    never recorded at the same time" is the answer to the question rather
    than a failure to answer it.
    """
    if not extents:
        raise WindowError("no series to draw")
    firsts = {name: lo for name, (lo, _hi) in extents.items()}
    lasts = {name: hi for name, (_lo, hi) in extents.items()}
    union = Bounds(min(firsts.values()), max(lasts.values()))
    if len(extents) < 2:
        return Drawable(union)

    start, end = max(firsts.values()), min(lasts.values())
    if start >= end:
        return Drawable(
            union,
            notes=(
                "these series were never recorded at the same time, so this "
                "window holds all of them and shows no two of them together",
            ),
            disjoint=True,
        )

    overlap = Bounds(start, end)
    notes: list[str] = []
    if overlap.span < union.span:
        outside = sorted(name for name in extents if firsts[name] < start or lasts[name] > end)
        verb = "runs" if len(outside) == 1 else "run"
        notes.append(
            f"opened on the span where every series has readings; {_and(outside)} {verb} beyond it"
        )
    return Drawable(overlap, notes=tuple(notes))


def window_bounds(window: Any) -> tuple[datetime, datetime]:
    """``(from, to)`` of an absolute window, or a refusal.

    Only the absolute shape can be zoomed by arithmetic. A run-relative window
    is measured from an event this module cannot see, so it is refused by name
    rather than guessed at — resolve it against its run first.
    """
    basis = str(getattr(window, "basis", "") or "absolute")
    if basis != "absolute":
        raise WindowError(
            f"a {basis!r} window cannot be zoomed here; it is measured from an "
            "event this does not know. Resolve it to absolute timestamps first"
        )
    fields = dict(getattr(window, "fields", None) or {})
    start, end = instant(fields.get("from")), instant(fields.get("to"))
    if start is None or end is None:
        raise WindowError(f"a window needs a readable 'from' and 'to'; got {fields!r}")
    if end <= start:
        raise WindowError("a window that ends before it begins has no span")
    return start, end


def span_seconds(window: Any) -> float:
    """How long a window is. ``0.0`` when there is no window to measure."""
    try:
        start, end = window_bounds(window)
    except WindowError:
        return 0.0
    return (end - start).total_seconds()


def _window(start: datetime, end: datetime):
    from .chart_spec import Window

    return Window(fields={"from": _stamp(start), "to": _stamp(end)})


def default_window(
    bounds: Bounds, *, span: timedelta = DEFAULT_SPAN, cadence: float = 0.0
) -> tuple[Any, list[str]]:
    """What to show before anybody has asked for a window.

    The most recent stretch of the readings, ANCHORED AT THE LAST ONE. A window
    measured from the clock is empty the moment ingestion stops, and an empty
    chart looks exactly like a working chart of a quiet signal.

    Shorter data is shown whole rather than padded, because empty space either
    side of a series reads as a gap in the readings.

    **How long a stretch depends on how often the channel is read.** It used to
    be one constant for every channel, and one constant cannot be right for
    both a channel sampled twice a second and one sampled once a day. The
    second of those opened on a day of its own record and drew ONE POINT — a
    figure that is not a figure, of a channel holding more than a year of
    history.

    So: *cadence* is the typical gap between readings, in seconds, and the
    window is widened until it would hold about as many readings as the chart
    aims to have marks. Never narrowed — a dense channel keeps the day, because
    a window sized to 800 readings of a 2 Hz signal is seven minutes, and
    nobody opening a figure wants to start there.
    """
    wanted = span.total_seconds()
    if cadence > 0:
        wanted = max(wanted, cadence * TARGET_MARKS)
    if bounds.span <= wanted:
        if bounds.span <= 0:
            # A record of ONE instant. Handing back `earliest == latest` is a
            # window with no width, which every consumer refuses — the spec
            # raises on `from == to` and the figure lane answered 500 on a
            # channel whose only fault was being new.
            #
            # Centred on the reading rather than starting at it: the reading is
            # what the reader came for, so it belongs in the middle of the frame
            # and not on its edge. The width is the narrowest a window is
            # allowed to be anywhere here, which is the same floor a zoom
            # clamps to.
            half = timedelta(seconds=MIN_MARKS * BUCKET_LADDER[0][1] / 2)
            return _window(bounds.earliest - half, bounds.latest + half), []
        return _window(bounds.earliest, bounds.latest), []
    start = bounds.latest - timedelta(seconds=wanted)
    stale = (datetime.now(UTC) - bounds.latest).total_seconds()
    notes: list[str] = []
    if stale > wanted:
        notes.append(
            f"the most recent reading is {_describe(stale)} old; this window "
            "ends where the readings do, not where the clock is"
        )
    return _window(start, bounds.latest), notes


def cadence_of(rows: int, span_s: float) -> float:
    """The typical seconds between readings, or 0 when it cannot be known.

    Deliberately crude — the mean gap, not a distribution. It is used to pick
    an opening window, where being out by a factor of two costs a reader one
    press of zoom, and a channel that samples in bursts would need its own
    study to do better.
    """
    if rows < 2 or span_s <= 0:
        return 0.0
    return span_s / (rows - 1)


def _describe(seconds: float) -> str:
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            count = int(seconds // size)
            return f"{count} {unit}{'s' if count != 1 else ''}"
    return f"{int(seconds)} seconds"


def _clamp(
    start: datetime,
    end: datetime,
    bounds: Bounds | None,
    finest: float,
) -> tuple[datetime, datetime, list[str]]:
    notes: list[str] = []
    floor = MIN_MARKS * finest
    span = (end - start).total_seconds()

    if span < floor:
        middle = start + (end - start) / 2
        half = timedelta(seconds=floor / 2)
        start, end = middle - half, middle + half
        notes.append(
            f"as far in as the readings go: a narrower window would hold fewer "
            f"than {MIN_MARKS} marks"
        )
        span = floor

    if bounds is not None:
        if span > bounds.span:
            notes.append("as far out as the readings go")
            return bounds.earliest, bounds.latest, notes
        if span == bounds.span:
            return bounds.earliest, bounds.latest, notes
        if start < bounds.earliest:
            start, end = bounds.earliest, bounds.earliest + timedelta(seconds=span)
            notes.append("at the first reading")
        if end > bounds.latest:
            end, start = bounds.latest, bounds.latest - timedelta(seconds=span)
            notes.append("at the last reading")
    return start, end, notes


def settle(
    window: Any,
    *,
    bounds: Bounds | None = None,
    finest: float = BUCKET_LADDER[0][1],
) -> tuple[Any, list[str]]:
    """A window somebody named, brought inside the readings.

    Zoom and pan have always been clamped — "a zoom never leaves the bounds"
    is what :class:`Bounds` is for. A window typed into a date picker was not,
    and it is the same kind of thing. So a reader could ask for January to
    September over a record of twenty-five days and get a figure seven-eighths
    empty, with a row of spans offering "last 15 minutes" because nothing
    longer than a week exists to offer: the view said months, the record said
    weeks, and nothing reconciled them.

    Same rules and the same words as a zoom that reaches the edge, so a window
    means one thing however it was arrived at.
    """
    start, end = window_bounds(window)
    start, end, notes = _clamp(start, end, bounds, finest)
    return _window(start, end), notes


def zoom(
    window: Any,
    *,
    factor: float = ZOOM_STEP,
    at: datetime | None = None,
    bounds: Bounds | None = None,
    finest: float = BUCKET_LADDER[0][1],
) -> tuple[Any, list[str]]:
    """A window *factor* times narrower, keeping *at* where it is.

    ``factor`` above one zooms in and below one zooms out, so a scroll wheel
    passes ``ZOOM_STEP`` one way and ``1 / ZOOM_STEP`` the other.

    ``at`` is the instant under the pointer. It stays at the same fraction
    across the plot, which is what makes a wheel feel like it is zooming
    towards something rather than towards the middle. Omitted, the middle
    holds still.
    """
    if factor <= 0:
        raise WindowError(f"a zoom factor must be positive; got {factor!r}")
    start, end = window_bounds(window)
    span = (end - start).total_seconds()
    anchor = at if at is not None else start + (end - start) / 2
    position = (anchor - start).total_seconds() / span
    position = min(1.0, max(0.0, position))

    new_span = span / factor
    new_start = anchor - timedelta(seconds=new_span * position)
    new_end = new_start + timedelta(seconds=new_span)
    new_start, new_end, notes = _clamp(new_start, new_end, bounds, finest)
    return _window(new_start, new_end), notes


def pan(
    window: Any,
    *,
    fraction: float,
    bounds: Bounds | None = None,
    finest: float = BUCKET_LADDER[0][1],
) -> tuple[Any, list[str]]:
    """The same span, moved by *fraction* of itself. Negative goes earlier."""
    start, end = window_bounds(window)
    shift = timedelta(seconds=(end - start).total_seconds() * fraction)
    new_start, new_end, notes = _clamp(start + shift, end + shift, bounds, finest)
    return _window(new_start, new_end), notes


def rebucketed(spec: Any, window: Any) -> Any:
    """*spec* moved to *window*, with a bucket that fits it.

    The bucket is re-chosen rather than carried, which is the whole reason zoom
    lives in the document: a narrower window earns finer marks, and a window
    that changed under an unchanged bucket draws the same coarse marks larger.

    A kind that declares no transform keeps none — a state has spans, and
    bucketing one by mean is the meaningless operation that kind exists to
    avoid.
    """
    from dataclasses import replace

    from .chart_spec import Transform

    if getattr(spec, "transform", None) is None:
        return replace(spec, window=window)
    agg = getattr(spec.transform, "agg", "mean")
    return replace(
        spec,
        window=window,
        transform=Transform(bucket=choose_bucket(span_seconds(window)), agg=agg),
    )
