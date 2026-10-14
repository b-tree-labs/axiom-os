# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A chart, drawn. SVG, no dependencies.

The terminal sparkline proves a kind renders; it does not make a picture
anybody would put in a paper. This is the picture.

SVG rather than a plotting library because a chart here has to be a
document: diffable, embeddable, styleable by a site's brand, and produced
without adding a plotting stack to a platform that runs on nuclear site
nodes. It is also text, which means a chart can be reviewed the way code
is.

**The design rules, and why each one is a rule.**

*Direct labels, no legend box.* A legend makes the reader look away from
the line, hold a colour in memory, and look back. Labelling each series at
its own end removes the lookup entirely. It is the single biggest
legibility win available and it costs nothing.

*Gaps are gaps.* A missing bucket breaks the line. Joining across one
draws a reading nobody took — the objection this codebase already makes to
interpolating a step-held channel.

*Modelled is dashed, where there is a measurement to mistake it for.* A
prediction must never look like a measurement at a glance — but on a figure
of nothing but predictions the dash separates nothing, and every line pays
for it. The word in the label carries provenance either way, and carries it
through a photocopy. Where both are present the space between them is
filled, because the divergence IS the subject.

*Units on the axis, once.* A number without its unit is not a
measurement, and repeating it on every tick is noise.

*No chartjunk.* No gridlines competing with the data, no gradients, no
shadows, no boxes. Ink is spent on readings.

*Each series on its own scale when units differ*, with the scale stated —
there is no shared axis between kW and degC and drawing one is a lie about
comparability.
"""

from __future__ import annotations

import html
import math
import re
import zlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from .chart_colour import assign_colours
from .numeric_format import format_number
from .units import UNDECLARED_BRIEF, declared
from .units import label as unit_label

#: Kept so a caller that pins a colour by hand has a house set to pin FROM;
#: nothing is drawn from this by default any more. A series' hue now comes from
#: what it MEASURES — see :mod:`.chart_colour`.
HOUSE_COLOURS = (
    "#0072b2",  # blue           (Okabe-Ito)
    "#4d4119",  # olive
    "#6991a7",  # slate blue
    "#009e73",  # bluish green   (Okabe-Ito)
    "#a34e78",  # magenta
    "#5b4a8a",  # violet
)

def themed(variable: str, literal: str) -> str:
    """A colour that adapts where it can and is fixed where it cannot.

    A figure is drawn ONCE, on a server, with no idea whether the page it
    lands on is light or dark — and the same bytes are also downloaded,
    dropped into a paper, and printed. Both have to be right.

    ``var(--name, #literal)`` is right in both. Inlined in a page, the
    custom property resolves and the figure takes the page's theme. Opened
    as a file, on a slide, or in a LaTeX document, there is no page to
    resolve it and the literal stands — which is the light figure we
    already draw, unchanged.

    And the OUTPUT is the same bytes either way, so the renderer stays
    deterministic: the theme is resolved by whoever is looking, not by
    whoever rendered.

    Only the chrome does this. The series colours say what a line MEASURES
    (see :mod:`.chart_colour`) — a hue that changed with the page would be
    a different claim about the data on a dark screen than on a light one.
    """
    return f"var({variable},{literal})"


#: `var(--name,#literal)` as the renderer writes it.
_THEMED = re.compile(r"var\(--[A-Za-z0-9-]+,\s*(#[0-9a-fA-F]{3,8}|[a-zA-Z]+)\)")


def flatten_theme(svg: str) -> str:
    """The same figure with every themed colour resolved to its literal.

    A browser resolves `var()`; almost nothing else does. cairosvg RAISES on
    one — ``invalid literal for int() with base 16: 'ar'`` — and Inkscape and
    the LaTeX toolchain around it are inconsistent about custom properties in
    presentation attributes. So a figure that is leaving the browser is
    flattened first.

    Which is not a compromise: the fallbacks ARE the figure we mean to
    publish. The theming exists so a figure sitting inside a dark page does
    not glare; a figure going into a paper, a slide or a PDF wants the light
    one, and that is what the literal says.

    Anything that is leaving — a download, a conversion, an embed served to
    somebody else's page — goes through here.
    """
    return _THEMED.sub(lambda m: m.group(1), svg)


INK = themed("--axk-plot-ink", "#1a1a1a")
MUTED = themed("--axk-plot-muted", "#6b7280")
#: An undeclared unit, so the gap is visible at a glance rather than only
#: on reading. Warmer than MUTED and distinctly not one of the series
#: colours — it marks an absence, it does not name a series.
UNDECLARED_INK = themed("--axk-plot-undeclared", "#a16207")

#: Space between the value axis's numbers and the plot, plus a little at
#: the canvas edge so the longest label is not flush against it.
AXIS_GUTTER = 16.0

#: Advance width of a digit as a fraction of the font size, for the sans
#: faces in LABEL_FONT. Enough to size a gutter; this is a layout budget,
#: not typesetting.
_DIGIT_EM = 0.62


def _text_width(text: str, size: float) -> float:
    return len(text) * size * _DIGIT_EM
RULE = themed("--axk-plot-rule", "#d8dce1")
PAPER = themed("--axk-plot-paper", "#ffffff")

#: A serif for numbers and a humanist sans for labels reads as a journal
#: figure rather than a dashboard tile.
FONT = "Charter,'Bitstream Charter','Iowan Old Style',Palatino,Georgia,'Times New Roman',serif"
LABEL_FONT = "'Inter','Helvetica Neue',Helvetica,Arial,sans-serif"


#: How a series may be drawn. Enumerated rather than open: a renderer that
#: silently accepts an unknown mark draws nothing and looks like a data problem.
MARKS = ("line", "bar", "scatter")


@dataclass
class Series:
    """One line: its name, its points, and what it is."""

    name: str
    points: list[tuple[datetime, float | None]]
    unit: str = ""
    modelled: bool = False
    colour: str = ""
    #: Paired measurement, when this is the model of one. The area between
    #: them is filled, because the divergence is the subject.
    against: str = ""
    #: How to draw it: ``line``, ``bar`` or ``scatter``. A property of the
    #: SERIES rather than of the spec, because a line, a bar and a scatter over
    #: a time axis are one chart drawn three ways rather than three kinds. An
    #: unknown value falls back to a line rather than drawing nothing, since a
    #: chart that silently renders empty is worse than one drawn plainly.
    mark: str = "line"


#: Journal figure widths, in millimetres. A figure is reproduced at the
#: column width of the journal, so it has to be DESIGNED at that width —
#: a 920-pixel chart shrunk to 89 mm has 4-point labels, which is how a
#: perfectly good figure becomes unreadable in print.
COLUMN_MM = {"single": 89.0, "double": 183.0, "screen": 244.0}

#: SVG user units per millimetre at the 96-dpi CSS reference.
_UNITS_PER_MM = 96.0 / 25.4


@dataclass
class Geometry:
    width: int = 920
    height: int = 420
    left: int = 68
    right: int = 132  # room for the direct labels
    top: int = 64
    bottom: int = 56
    #: Series whose values sit closer together than this fraction of the
    #: plot height get their labels nudged apart rather than overlapped.
    label_gap: float = 0.032
    #: Multiplies every type size and stroke. Type does not scale linearly
    #: with a figure: halve the width and 6-point text is unreadable, so a
    #: narrow figure gets proportionally LARGER type, which is what every
    #: journal style guide asks for and what eyeballing a shrunk PNG hides.
    scale: float = 1.0

    @classmethod
    def for_column(cls, column: str = "double", *, ratio: float = 0.46) -> Geometry:
        """A figure sized for a journal column.

        ``ratio`` is height over width; 0.46 is close to the golden
        section and is the proportion most line figures are set at.
        """
        try:
            millimetres = COLUMN_MM[column]
        except KeyError:
            known = ", ".join(sorted(COLUMN_MM))
            raise ValueError(f"no column width {column!r}; have: {known}") from None
        width = int(round(millimetres * _UNITS_PER_MM))
        # Type shrinks with the figure, but sub-linearly, so a narrow
        # figure keeps type ABOVE the journal legibility floor while the
        # plot still gets the room.
        #
        # The first cut had this inverted — narrow figures got LARGER type
        # — and produced a 336-unit canvas carrying 33-unit text: title
        # clipped off the edge, ticks overlapping into a blob, the plot
        # squashed to a sliver. It rendered without error and was
        # unusable, which is the only kind of bug a chart has.
        scale = (millimetres / COLUMN_MM["screen"]) ** 0.45
        return cls(
            width=width,
            height=int(round(width * ratio)),
            left=int(round(52 * scale)),
            right=int(round(92 * scale)),
            top=int(round(48 * scale)),
            bottom=int(round(40 * scale)),
            scale=scale,
        )


    #: The widest and narrowest canvas a caller may ask for. A width comes
    #: from a browser measuring an element, so it can arrive as 0 during
    #: layout or as something enormous from a bad calculation, and neither
    #: should reach the renderer.
    MIN_WIDTH = 360
    MAX_WIDTH = 2400

    @classmethod
    def for_width(cls, width: int, *, height: int = 0, ratio: float = 0.46) -> Geometry:
        """A figure sized to the space it is being drawn into, at the SAME
        type size as every other figure.

        This is not `for_column`, and the difference is the point. A journal
        column is a fixed physical width, so type there scales with the
        figure or it stops being legible in print. A panel on a screen is
        whatever width the window happens to be, and type that grew with it
        would mean the same label was one size in a two-up grid and another
        at full screen.

        Ben, on a full-screen figure: "the font sizes should stay consistent
        across the whole app always." So `scale` stays at 1 and the extra
        width becomes PLOT — which is what somebody wants from a bigger
        figure. What they do not want is the same picture magnified, which
        is what stretching a 920-unit SVG across 1850 pixels does to every
        glyph and every stroke at once.
        """
        width = max(cls.MIN_WIDTH, min(int(width), cls.MAX_WIDTH))
        return cls(
            width=width,
            height=max(220, int(height) if height else int(round(width * ratio))),
        )


def _sz(base: float, geo: Geometry) -> str:
    """A type size or stroke width at this figure's scale."""
    return f"{base * getattr(geo, 'scale', 1.0):.2f}"


def _places_for(span: float) -> int:
    """Decimal places that resolve *span* without inventing precision."""
    if span <= 0:
        return 2
    import math as _math

    return max(0, min(4, 2 - int(_math.floor(_math.log10(span)))))


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


#: Engineering prefixes. A reactor at 1,000,000 W is a reactor at 1 MW,
#: and an axis that says the former has made the reader do arithmetic to
#: learn something the axis already knew.
_PREFIXES = (
    (1e9, "G"), (1e6, "M"), (1e3, "k"),
    (1.0, ""), (1e-3, "m"), (1e-6, "µ"),
)


def scale_for(high: float, unit: str) -> tuple[float, str]:
    """``(divisor, prefixed unit)`` for an axis topping out at *high*.

    Only for units a prefix means something on. Prefixing a percentage or
    a count produces "kpct", which is not a unit anybody uses — the test
    is whether the unit is one an SI prefix attaches to, not whether the
    number is large.
    """
    bare = (unit or "").strip()
    if not bare or bare.lower() in _UNPREFIXED:
        return 1.0, bare
    magnitude = abs(high)
    for factor, prefix in _PREFIXES:
        if magnitude >= factor:
            return factor, f"{prefix}{bare}"
    return 1.0, bare


#: Units an SI prefix must not be glued to.
_UNPREFIXED = frozenset({
    "%", "pct", "percent", "degc", "degf", "c", "f", "k",
    "step", "steps", "count", "n", "ph", "rpm",
    # An instrument's own scale. "k console_units" is not a quantity.
    "console_units", "console_unit",
})


def _axis_scale(
    unit: str,
    extent: tuple[float, float],
    *,
    log: bool = False,
    declared: str | None = None,
) -> tuple[float, str]:
    """``(divisor, unit)`` for a value axis.

    Two things decide which prefix the axis says. A `declared` unit wins: the
    question being asked establishes it, or a panel has to match its neighbour,
    or a site says its power is in kilowatts. Nothing else may quietly override
    a caller who asked for kilowatts, so a declaration that is not this unit is
    refused by name rather than ignored.

    Otherwise it is inferred from `extent`, which is the DATA's own range and
    not the axis bounds. The bounds are padded, and a maximum sitting a few
    percent below a decade gets pushed over it: 946,000 W rendered as
    "0.946 MW" because the padded bound crossed a million, not because any
    reading did. "0.946 MW" leads with a zero, which is the one thing a prefix
    exists to remove.

    A logarithmic axis takes no prefix at all. Across ten decades a prefix only
    shifts every exponent by a constant, labelling the bottom of the axis
    "1e-10 MW" for a reading of a tenth of a milliwatt. There the decades
    already do the prefix's job.
    """
    bare = (unit or "").strip()
    if log:
        if declared and declared.strip() != bare:
            raise ValueError(
                f"cannot display {bare!r} as {declared!r}: the axis is "
                f"logarithmic, and a prefix there only shifts every exponent"
            )
        return 1.0, bare
    if declared is not None:
        return _declared_scale(bare, declared)
    return scale_for(max(abs(extent[0]), abs(extent[1])), bare)


def _declared_scale(bare: str, declared: str) -> tuple[float, str]:
    """``(divisor, unit)`` for a unit the caller asked for, or a refusal."""
    want = declared.strip()
    if want == bare:
        return 1.0, bare
    if bare.lower() in _UNPREFIXED or not bare:
        raise ValueError(
            f"cannot display {bare!r} as {want!r}: {bare!r} takes no SI prefix"
        )
    for factor, prefix in _PREFIXES:
        if prefix and want == f"{prefix}{bare}":
            return factor, want
    known = ", ".join(f"{p}{bare}" for _, p in _PREFIXES if p)
    raise ValueError(
        f"cannot display {bare!r} as {want!r}; it is one of: {bare}, {known}"
    )


def _nice_ticks(low: float, high: float, count: int = 4) -> list[float]:
    """Round numbers a reader recognises, covering [low, high].

    A tick at 0.3847 is a number the axis invented. Ticks are chosen from
    1/2/5 x 10^n so every label is one somebody would say out loud.
    """
    if high <= low:
        return [low]
    raw = (high - low) / max(count, 1)
    magnitude = 10 ** (len(str(int(abs(raw)))) - 1) if abs(raw) >= 1 else 1.0
    while magnitude > abs(raw):
        magnitude /= 10
    ladder = (1, 2, 5, 10)
    for step in ladder:
        if magnitude * step >= raw:
            step_size = magnitude * step
            break
    else:
        step_size = magnitude * 10
    # The rule above takes the FIRST step at or above the ideal spacing, which
    # keeps the count at or below `count` — and over a span like 0..363 it
    # lands on 200 where 100 fits, leaving an axis labelled 0 and 200 with the
    # data topping out at 363. Two ticks is not a scale; the reader has to
    # extrapolate past the last one. So when a step leaves fewer than three
    # ticks inside the range, drop one rung.
    def _within(size: float) -> int:
        # Counted over [low, high] and not over the generated list, because a
        # tick past `high` is generated and then clipped by the plot: the first
        # cut of this guard counted the clipped one and left the axis at two.
        first = (low // size) * size
        return sum(
            1 for i in range(int((high - first) / size) + 2)
            if low <= first + i * size <= high
        )

    if _within(step_size) < 3:
        rung = ladder.index(int(round(step_size / magnitude))) if magnitude else 0
        if rung > 0:
            step_size = magnitude * ladder[rung - 1]
    start = (low // step_size) * step_size
    ticks = []
    value = start
    while value <= high + step_size * 0.5:
        if value >= low - step_size * 0.001:
            ticks.append(round(value, 10))
        value += step_size
    return ticks or [low, high]


#: Round intervals a clock actually shows, smallest first.
#: Rungs a clock has names for. The gaps matter as much as the rungs: between
#: one minute and five there was nothing, so a fifteen-minute window that fell
#: just short of three five-minute ticks dropped a whole rung and drew FIFTEEN
#: labels, one a minute, overlapping each other. Two and three minutes close
#: that gap, and two hours closes the same one higher up.
_TIME_STEPS = (
    1, 5, 15, 30, 60, 120, 180, 300, 600, 900, 1800,
    3600, 2 * 3600, 3 * 3600, 6 * 3600, 12 * 3600,
    86400, 2 * 86400, 7 * 86400, 14 * 86400, 30 * 86400, 90 * 86400, 365 * 86400,
)


def _time_ticks(
    start: datetime, end: datetime, count: int = 5
) -> tuple[list[datetime], float]:
    """Ticks at round clock times, not at even fractions of the window.

    Dividing the span by five gives 14:01:14, which is a time the axis
    invented — the same objection as a value tick at 0.3847. A reader
    orients on times they recognise.
    """
    from datetime import timedelta

    span = (end - start).total_seconds()
    if span <= 0:
        return [start], 1.0
    # The step nearest the target tick count, not the first one big enough.
    # "First >= ideal" overshot a five-minute window to a five-minute step
    # and drew a single tick — an axis with one label is not an axis.
    wanted = max(count - 1, 1)
    step = min(_TIME_STEPS, key=lambda candidate: abs(span / candidate - wanted))
    # ...but never so coarse that the axis has nothing on it. "Nearest to the
    # wanted count" picked a SEVEN-DAY step for a nine-day window, drawing one
    # label; an axis with one label does not say where anything is. Three is
    # the floor, the same floor the value axis keeps.
    while span / step < 3 and _TIME_STEPS.index(step) > 0:
        step = _TIME_STEPS[_TIME_STEPS.index(step) - 1]
    epoch = start.replace(microsecond=0)
    offset = (
        epoch.timestamp() % step
    )
    first = epoch + timedelta(seconds=(step - offset) if offset else 0)
    ticks = []
    moment = first
    while moment <= end:
        ticks.append(moment)
        moment += timedelta(seconds=step)
    # Always show where the window begins, even when it is not on a round
    # tick: "from when" is the first thing a reader asks.
    if not ticks or (ticks[0] - start).total_seconds() > step * 0.35:
        ticks.insert(0, start)
    return (ticks or [start, end]), float(step)


def _fmt_time(moment: datetime, step_seconds: float) -> str:
    """As much of the timestamp as the SPACING makes meaningful.

    Keyed to the step rather than the window: a day-long window ticked
    every six hours and labelled by day reads "20 Sep, 20 Sep, 21 Sep" —
    three labels, two of them identical, and no way to tell which is which.
    The resolution of a label has to match the distance between labels.
    """
    if step_seconds < 60:
        return moment.strftime("%H:%M:%S")
    if step_seconds < 86400:
        return moment.strftime("%H:%M")
    if step_seconds < 86400 * 28:
        return moment.strftime("%d %b")
    if step_seconds < 86400 * 300:
        return moment.strftime("%b %Y")
    return moment.strftime("%Y")


def _time_labels(ticks: list[datetime], step_seconds: float) -> list[tuple[str, str]]:
    """``(label, date)`` per tick; ``date`` is "" unless the tick needs one.

    A two-day window ticked every twelve hours labelled itself
    ``00:00 · 12:00 · 00:00 · 12:00`` — the same four labels twice over, with
    nothing anywhere saying which day either midnight belonged to. Zooming
    redrew it faithfully and it looked like nothing had happened, because the
    labels that came back were the labels that left.

    So a tick carries its date when the date is not obvious: any tick that
    starts a different day from the one before it, and — the rule that
    matters most — **the two ends of the axis**.

    The ends carry whatever the interior labels leave out. Clock times leave
    out the day, so the first tick names its day and the last names its own
    when the window crosses into another. Day-and-month labels leave out the
    year, so the ends carry the year instead. Once a label is already
    ``Sep 2026`` or ``2026``, nothing is missing and the ends stay bare.

    Before this, a window that sat inside one day carried no date anywhere:
    the axis read ``14:06 · 15:00 · 16:00``, and the only way to learn WHICH
    afternoon that was, was to decode the ISO timestamps in the provenance
    line under the plot. "Which day am I looking at" is not a question a
    reader should have to parse a timestamp to answer.
    """
    if not ticks:
        return []

    if step_seconds >= 86400:
        labels = [_fmt_time(t, step_seconds) for t in ticks]
        if step_seconds >= 86400 * 28:
            # "%b %Y" and "%Y" already say the year. Nothing is missing.
            return [(label, "") for label in labels]
        # "%d %b" — the year is the part the reader cannot recover.
        out = [(label, "") for label in labels]
        out[0] = (labels[0], ticks[0].strftime("%Y"))
        if ticks[-1].year != ticks[0].year:
            out[-1] = (labels[-1], ticks[-1].strftime("%Y"))
        return out

    out = []
    previous = None
    for tick in ticks:
        label = _fmt_time(tick, step_seconds)
        changed = previous is not None and tick.date() != previous
        out.append((label, tick.strftime("%d %b") if changed else ""))
        previous = tick.date()
    # The ends, last, so they win over the day-change rule rather than
    # fighting it: the first tick always names its day, and the last names
    # its own only when it is a different one. A window inside a single day
    # gets one date, at the left, and no repetition.
    out[0] = (out[0][0], ticks[0].strftime("%d %b"))
    if ticks[-1].date() != ticks[0].date():
        out[-1] = (out[-1][0], ticks[-1].strftime("%d %b"))
    return out


@dataclass(frozen=True)
class AxisPlan:
    """Which series a figure can draw, and on which axis."""

    #: the unit the left axis belongs to
    leading: str = ""
    #: the unit the right axis belongs to, or "" for none
    secondary: str = ""
    drawn: tuple[Series, ...] = ()
    #: left out, because a third unit has nowhere to go
    dropped: tuple[Series, ...] = ()
    #: drawn against a unit they do not declare. Said out loud: the figure is
    #: showing them, and nothing has claimed what they are measured in.
    unverified: tuple[str, ...] = ()


def axis_selection(
    series: list[Series], *, secondary_unit: str = ""
) -> AxisPlan:
    """Which series a figure can draw, and which unit gets which axis.

    A renderer shows one unit per labelled axis, and a second only when there
    is a second axis to put it on, because two units against ONE labelled axis
    is the most confidently wrong thing a chart can do. Something therefore
    gets left out, and a caller reporting on the figure needs the same answer
    the figure reached rather than its own guess at it.

    Three rules, in the order they were learned.

    **A declaration outranks an absence.** Which unit leads used to be simply
    the first series'. So a channel with no declared unit, returned first, took
    the axis and every channel that DID declare one was left out — a
    measurement whose map entry is missing sidelining the model of it. After a
    declaration, the unit the most series share, and after that the first.

    **An absence JOINS the declaration rather than being excluded by it.**
    Dropping the undeclared ones was still wrong, just in the other direction:
    asked to draw a measurement against two models of it, the figure drew one
    line, because two of the three had no map entry. An undeclared unit is not
    a claim that the quantity is different. It is the absence of any claim, and
    the reader asking for the comparison is the one making the case. So they
    are drawn, on the one declared axis, and NAMED as unverified — which is
    more honest than a figure that quietly showed a third of what was asked
    for. With two declared units there is no single axis to join, so they are
    left out and said.

    **A second unit takes the second axis by itself.** It used to require the
    caller to name it, and a caller that does not know which unit will lead
    cannot name the other one. Two units against two LABELLED axes is a figure
    that reads correctly, and it is the one a reader asking for a temperature
    beside a flow rate wants. A third is still refused: nobody holds three
    scales.
    """
    present = [s.unit for s in series if any(v is not None for _, v in s.points)]
    declared_units = [u for u in present if u.strip()]
    leading = (
        max(
            set(declared_units),
            key=lambda u: (declared_units.count(u), -declared_units.index(u)),
        )
        if declared_units
        else ""
    )
    # The runner-up by the same rule as the leader — the unit the most
    # remaining series share, then the first seen. Ordering them by first
    # appearance alone handed the right axis to whichever unit happened to
    # sort first, which is an accident of the channel names.
    rest = sorted(
        (u for u in dict.fromkeys(declared_units) if u != leading),
        key=lambda u: (-declared_units.count(u), declared_units.index(u)),
    )
    # Asked for by name when it is one the figure actually carries; otherwise
    # the next declared unit, which is what makes two quantities just work.
    second = (
        secondary_unit
        if secondary_unit and secondary_unit in rest
        else (rest[0] if rest else "")
    )

    kept = {leading} | ({second} if second else set())
    # The undeclared join the axis only when there is exactly one to join.
    joinable = bool(leading) and not second
    unverified = (
        tuple(sorted({s.name for s in series if not s.unit.strip()})) if joinable else ()
    )
    if joinable:
        kept.add("")

    return AxisPlan(
        leading=leading,
        secondary=second,
        drawn=tuple(s for s in series if s.unit in kept),
        dropped=tuple(s for s in series if s.unit not in kept),
        unverified=unverified,
    )


def _and(names: list[str], cap: int = 3) -> str:
    """``a``, ``a and b``, ``a, b and c``, ``a, b and 4 more``."""
    if len(names) > cap:
        return f"{', '.join(names[: cap - 1])} and {len(names) - cap + 1} more"
    if len(names) <= 1:
        return names[0] if names else ""
    return f"{', '.join(names[:-1])} and {names[-1]}"


def title_for(series: list[Series]) -> str:
    """What a figure is OF, said from what it draws.

    A surface that titles its figure with the site is titling it with something
    the page already says, usually twice: once in the heading a reader chose
    the site from, and again at the top of the picture. The figure's own title
    is the one place that can say what is IN it.

    A measurement beside models of it is the shape worth naming, because that
    is what the reader came to see. Otherwise the channels, capped, because a
    title of nine names is a list rather than a title.
    """
    measured = [s.name for s in series if not s.modelled]
    modelled = [s.name for s in series if s.modelled]
    if measured and modelled:
        return f"{_and(measured)} against {_and(modelled)}"
    return _and(measured or modelled)


def render_svg(
    series: list[Series],
    *,
    title: str = "",
    subtitle: str = "",
    provenance: str = "",
    geometry: Geometry | None = None,
    secondary_unit: str = "",
    log_units: tuple[str, ...] = (),
    display_units: Mapping[str, str] | None = None,
    series_colours: Mapping[str, str] | None = None,
    preferred_colours: Mapping[str, str] | None = None,
    held_extent: Mapping[str, tuple[float, float]] | None = None,
) -> str:
    """The chart, as an SVG document.

    ``held_extent`` pins an axis to a range the caller already knows, instead
    of letting the window's own readings decide it. That is what makes a
    sweep through time watchable: re-scaling on every step makes the trace
    jump vertically while the reader is trying to follow it move sideways,
    and the thing they are watching stops being the data.
    """
    geo = geometry or Geometry()

    # Room for what is actually drawn. A margin scaled from a constant
    # fitted the screen preset and collided at every other size: the title
    # sat inside the plot, the provenance line landed on the time ticks,
    # and two direct labels overlapped each other.
    line = 19.0 * geo.scale          # a heading line
    small = 13.5 * geo.scale         # a label line
    geo.top = int(round(
        (line if title else 0) + (small * 1.35 if subtitle else 0) + small * 1.6
    ))
    mixed_units = len({s.unit for s in series
                       if any(v is not None for _, v in s.points)}) > 1
    geo.bottom = int(round(
        small * 1.7 + (small * 1.7 if provenance else 0)
        + (small * 1.2 if mixed_units else 0) + 6
    ))
    # A direct label is TWO lines — a name and its value — so the gap that
    # keeps two of them apart is two lines, not an arbitrary fraction.
    # A label is a name and its value, and the two have to read as one
    # block. The space BETWEEN blocks must beat the space inside one, or
    # a value sits as close to the next name as to its own and the reader
    # has to guess which line it belongs to. Two and a half times the
    # internal gap is the smallest ratio that reads unambiguously.
    geo.label_gap = (small * 1.45) / max(geo.height - geo.top - geo.bottom, 1)

    if geometry is None or True:
        # Real channel names are `NCDT1:HEAT:TC-CP1_1`, and a fixed margin
        # clipped one mid-word — a chart that truncates the name of the
        # thing it is drawing. Measured from the labels themselves, and
        # capped so one very long name cannot squeeze the plot away.
        longest = max(
            (len(s.name) + (8 if s.modelled else 0) + 11 for s in series),
            default=0,
        )
        geo.right = int(min(max(96, longest * 7.1 + 24), geo.width * 0.42))
    plot_w = geo.width - geo.left - geo.right
    plot_h = geo.height - geo.top - geo.bottom

    stamps = [t for s in series for t, v in s.points if v is not None]
    if not stamps:
        return _empty(geo, title, "no readings to draw")

    # One unit per figure. Each series already gets its own scale, and
    # only ONE axis is drawn — so a temperature overlaid on a power axis
    # is read against watts. Drawing them together and labelling one of
    # them is the most confidently wrong thing this renderer could do, and
    # the module docstring already said not to.
    #
    # The second unit is not silently dropped: the caller is told, so it
    # can draw a second figure, which is what a journal would print.
    # A SECOND axis is opt-in, and the objection above is answered by drawing
    # it. What must not happen is two units against one labelled axis; two
    # units against two labelled axes is the figure the analytics dashboard
    # draws for integrated energy, and it reads correctly because each side
    # says what it is. Everything beyond two is still refused, because a
    # reader cannot hold three scales.
    plan = axis_selection(series, secondary_unit=secondary_unit)
    leading, secondary_unit = plan.leading, plan.secondary
    left_out = sorted({s.unit for s in plan.dropped})
    dropped_names = [s.name for s in plan.dropped]
    # Joining the axis means sharing its SCALE, not merely being drawn beside
    # it. Left with a unit of its own, an unverified series got its own scale
    # under a label that named somebody else's, which is two units against one
    # labelled axis — the exact thing this renderer refuses to do.
    unverified = set(plan.unverified)
    series = [
        replace(s, unit=plan.leading) if s.name in unverified else s
        for s in plan.drawn
    ]
    t0, t1 = min(stamps), max(stamps)
    span = max((t1 - t0).total_seconds(), 1e-9)

    # One scale per UNIT. Two channels in degC share an axis and mean it;
    # kW against degC does not.
    units = {}
    # The data's own range, kept because the prefix is chosen from it. `units`
    # gets padded below, and a maximum a few percent under a decade would be
    # pushed over it: 946,000 W read as "0.946 MW" because the BOUND crossed a
    # million, not because any reading did.
    extent: dict[str, tuple[float, float]] = {}
    unplottable = 0  # readings a log axis cannot place
    for s in series:
        values = [v for _, v in s.points if v is not None]
        if s.unit in log_units:
            # A logarithm of zero or less is not a small number, it is no
            # number. Such a reading becomes a GAP, exactly like a missing one,
            # and it is counted so the figure can say how many it could not
            # place. Dropping them quietly would be the same lie as joining a
            # line across a reading nobody took.
            unplottable += sum(1 for v in values if v <= 0)
            values = [v for v in values if v > 0]
        if not values:
            continue
        low, high = min(values), max(values)
        # A held range wins over what this window happens to contain. The
        # caller measured it over a span this one is a part of, so widening it
        # to fit these readings would be widening it to fit a subset.
        pinned = (held_extent or {}).get(s.unit)
        if pinned:
            low, high = min(low, pinned[0]), max(high, pinned[1])
            low, high = pinned[0], pinned[1]
        got = units.setdefault(s.unit, [low, high])
        got[0], got[1] = min(got[0], low), max(got[1], high)
        seen = extent.setdefault(s.unit, (low, high))
        extent[s.unit] = (min(seen[0], low), max(seen[1], high))
    for unit, bounds in units.items():
        if unit in log_units:
            # Pad in decades, not in linear span.
            bounds[0] = bounds[0] / 2.0
            bounds[1] = bounds[1] * 2.0
            continue
        low, high = bounds
        if low == high:
            # A flat line drawn on the floor reads as zero.
            pad = abs(low) * 0.1 or 1.0
            bounds[0], bounds[1] = low - pad, high + pad
        else:
            pad = (high - low) * 0.08
            bounds[0], bounds[1] = low - pad, high + pad

    def _ticks_for(unit: str) -> list[float]:
        """Decades on a log axis, ordinary nice steps otherwise. A log axis
        ticked linearly is unreadable: the marks bunch at the top."""
        low, high = units[unit]
        if unit not in log_units:
            return list(_nice_ticks(low, high))
        lo, hi = math.floor(math.log10(max(low, 1e-300))), math.ceil(math.log10(max(high, 1e-300)))
        return [10.0 ** e for e in range(int(lo), int(hi) + 1)]

    # Every declared unit resolved ONCE, before anything is measured or drawn,
    # so a refusal happens before half a figure exists. A declaration naming a
    # unit this figure does not carry is simply unused: a caller may hold one
    # setting for a whole dashboard, and only some panels draw watts.
    declarations = dict(display_units or {})
    shown: dict[str, tuple[float, str]] = {
        unit: _axis_scale(unit, extent.get(unit, bounds), log=unit in log_units,
                          declared=declarations.get(unit))
        for unit, bounds in ((u, (b[0], b[1])) for u, b in units.items())
    }

    # The left gutter has to fit what is drawn in it. It was a fixed 68
    # units, and a right-anchored tick label wider than that ran off the
    # canvas: a value axis reading "000000" where it meant 1000000.
    #
    # It bit here because an undeclared unit cannot take an SI prefix —
    # there is no unit to prefix — so those axes keep their full-width
    # numbers instead of being scaled to "1 M". That is the right call for
    # the number and the wrong one for a gutter that never measured itself.
    _primary = next(iter(units), "")
    if _primary in units:
        _divisor, _axis_unit = shown[_primary]
        _labels = [
            format_number(round(tick / _divisor, 10))
            for tick in _ticks_for(_primary)
        ]
        _labels.append(unit_label(_axis_unit, brief=True))
        # `_sz` formats for the SVG attribute; the measurement wants the
        # number, so the scale is applied here rather than parsed back out.
        needed = (
            _text_width(max(_labels, key=len), 11.5 * getattr(geo, 'scale', 1.0))
            + AXIS_GUTTER
        )
        if needed > geo.left:
            geo = replace(geo, left=int(math.ceil(needed)))
            plot_w = geo.width - geo.left - geo.right

    # The right gutter, when a second axis was asked for, measured the same
    # way. This is not decoration: the direct labels at the end of each line
    # live in the right margin, and an axis drawn into that margin without
    # reserving room lands its numbers on top of the series names. The figure
    # still renders, which is the only kind of bug a chart has.
    #
    # The gutter comes out of the PLOT, not the canvas, so asking for a second
    # axis never changes the size of the image.
    _primary = next(iter(units), "")
    second = (
        secondary_unit
        if secondary_unit and secondary_unit in units and secondary_unit != _primary
        else ""
    )
    right_gutter = 0.0
    if second:
        _s_divisor, _s_axis_unit = shown[second]
        _s_labels = [
            format_number(round(tick / _s_divisor, 10)) for tick in _ticks_for(second)
        ]
        _s_labels.append(unit_label(_s_axis_unit, brief=True))
        right_gutter = (
            _text_width(max(_s_labels, key=len), 11.5 * getattr(geo, "scale", 1.0))
            + AXIS_GUTTER
        )
        plot_w = geo.width - geo.left - geo.right - right_gutter

    def x_of(moment: datetime) -> float:
        return geo.left + plot_w * ((moment - t0).total_seconds() / span)

    def y_of(value: float, unit: str) -> float:
        low, high = units[unit]
        if unit in log_units:
            # Positions are placed by their logarithm; a non-positive reading
            # has no position at all and is treated as absent by the callers,
            # which is why this never sees one.
            lo, hi = math.log10(max(low, 1e-300)), math.log10(max(high, 1e-300))
            v = math.log10(max(value, 1e-300))
            return geo.top + plot_h * (1 - (v - lo) / ((hi - lo) or 1))
        return geo.top + plot_h * (1 - (value - low) / (high - low or 1))

    def plottable(value: float | None, unit: str) -> bool:
        """A reading this axis can place. On a log axis, zero and below cannot
        be placed and become gaps rather than being pushed onto the floor."""
        if value is None:
            return False
        return not (unit in log_units and value <= 0)

    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{geo.width}" '
        f'height="{geo.height}" viewBox="0 0 {geo.width} {geo.height}" '
        f'font-family="{FONT}">',
        f'<rect width="{geo.width}" height="{geo.height}" fill="{PAPER}"/>',
    ]

    cursor = line
    if title:
        out.append(
            f'<text x="{geo.left}" y="{cursor:.1f}" font-size="{_sz(19, geo)}" '
            f'fill="{INK}" font-family="{LABEL_FONT}" font-weight="600">'
            f'{_esc(title)}</text>'
        )
        cursor += small * 1.35
    if subtitle:
        out.append(
            f'<text x="{geo.left}" y="{cursor:.1f}" font-size="{_sz(12.5, geo)}" '
            f'fill="{MUTED}" font-family="{LABEL_FONT}">{_esc(subtitle)}</text>'
        )

    # The value axis: one per unit, the first on the left, scaled so its
    # numbers are ones a person would say out loud.
    primary = next(iter(units), "")
    divisor, axis_unit = shown[primary]
    for tick in _ticks_for(primary):
        y = y_of(tick, primary)
        if not (geo.top - 1 <= y <= geo.top + plot_h + 1):
            continue
        out.append(
            f'<line x1="{geo.left}" y1="{y:.1f}" x2="{geo.left + plot_w}" '
            f'y2="{y:.1f}" stroke="{RULE}" stroke-width="{_sz(1, geo)}"/>'
        )
        out.append(
            f'<text x="{geo.left - 10}" y="{y + 4:.1f}" font-size="{_sz(11.5, geo)}" '
            f'fill="{MUTED}" text-anchor="end">'
            f'{_esc(format_number(round(tick / divisor, 10)))}</text>'
        )
    # Above the topmost tick and right-aligned with the numbers, so it
    # reads as their unit rather than as part of the subtitle.
    #
    # Drawn UNCONDITIONALLY. This used to be `if axis_unit:`, so a series
    # whose unit nobody declared got an axis of bare numbers — and a bare
    # axis is not a missing label, it is a claim that the numbers need no
    # unit. On a live install that was true of every reading from a site.
    out.append(
        f'<text x="{geo.left - 10}" y="{geo.top - 9}" font-size="{_sz(11, geo)}" '
        f'fill="{MUTED if declared(axis_unit) else UNDECLARED_INK}" '
        f'text-anchor="end" font-family="{LABEL_FONT}" '
        f'letter-spacing="0.05em">{_esc(unit_label(axis_unit, brief=True))}</text>'
    )

    # The SECOND value axis, on the right, when one was asked for.
    #
    # This is what makes two units in one figure honest. Each side carries its
    # own numbers and its own unit, so a reader looking at a line knows which
    # scale it belongs to instead of reading a temperature against watts. No
    # gridlines are drawn from this side: a second set of rules crossing the
    # first turns the plot into graph paper and neither set ends up readable.
    if second:
        s_div, s_unit = _s_divisor, _s_axis_unit
        right_x = geo.left + plot_w
        out.append(
            f'<line x1="{right_x}" y1="{geo.top}" x2="{right_x}" '
            f'y2="{geo.top + plot_h}" stroke="{INK}" stroke-width="{_sz(1.1, geo)}"/>'
        )
        for tick in _ticks_for(second):
            y = y_of(tick, second)
            if not (geo.top - 1 <= y <= geo.top + plot_h + 1):
                continue
            out.append(
                f'<line x1="{right_x}" y1="{y:.1f}" x2="{right_x + 4}" '
                f'y2="{y:.1f}" stroke="{INK}" stroke-width="{_sz(1, geo)}"/>'
            )
            out.append(
                f'<text x="{right_x + 8}" y="{y + 4:.1f}" '
                f'font-size="{_sz(11.5, geo)}" fill="{MUTED}" text-anchor="start">'
                f'{_esc(format_number(round(tick / s_div, 10)))}</text>'
            )
        out.append(
            f'<text x="{right_x + 8}" y="{geo.top - 9}" font-size="{_sz(11, geo)}" '
            f'fill="{MUTED if declared(s_unit) else UNDECLARED_INK}" '
            f'text-anchor="start" font-family="{LABEL_FONT}" '
            f'letter-spacing="0.05em">{_esc(unit_label(s_unit, brief=True))}</text>'
        )

    # The time axis.
    ticks, step = _time_ticks(t0, t1)
    labelled = dict(zip(ticks, _time_labels(ticks, step), strict=True))
    # Thin them until they FIT. Round clock times are chosen without knowing
    # how wide the figure is, so a narrow one drew `12:48` on top of `12:49`:
    # two labels a reader cannot separate are worse than one they can.
    #
    # Measured against the label, not against a fixed count — `12:48` and
    # `Sep 2026` need different room, and a figure sized for a journal column
    # needs more of it than one on a screen.
    tick_size = 11.5 * geo.scale
    widest = max((len(labelled[m][0]) for m in ticks), default=4)
    # The label's ink, plus a clear gap the width of about four characters.
    # Labels that merely fail to overlap still read as one run of digits.
    needs = widest * tick_size * 0.62 + tick_size * 2.4
    kept_ticks = [ticks[0]] if ticks else []
    for moment in ticks[1:]:
        if x_of(moment) - x_of(kept_ticks[-1]) >= needs:
            kept_ticks.append(moment)
    # The last one earns its place: it is the end of the window, and dropping
    # it leaves the axis stopping short of the data. If it crowds its
    # neighbour, the NEIGHBOUR goes.
    if len(kept_ticks) > 1 and ticks and kept_ticks[-1] is not ticks[-1]:
        while len(kept_ticks) > 1 and x_of(ticks[-1]) - x_of(kept_ticks[-1]) < needs:
            kept_ticks.pop()
        kept_ticks.append(ticks[-1])
    ticks = kept_ticks
    # Grouped and named so a surface can move them.
    #
    # Panning is the reader dragging the DATA. The trace and the times under
    # it are one thing to a hand on a trackpad, and a frame that slides while
    # its labels stay put reads as a rendering fault rather than a pan. The
    # axis LINE is not in here: it is the frame, and the frame holds still.
    out.append('<g class="axk-xticks">')
    for moment in ticks:
        x = x_of(moment)
        anchor = "start" if moment == ticks[0] else (
            "end" if moment == ticks[-1] else "middle"
        )
        out.append(
            f'<text x="{x:.1f}" y="{geo.top + plot_h + small * 1.45:.1f}" font-size="{_sz(11.5, geo)}" '
            f'fill="{MUTED}" text-anchor="{anchor}">'
            f'{_esc(labelled[moment][0])}'
            # The date on a second line, only where the day changes. Beside the
            # time it doubles the label width and the ticks collide; beneath it,
            # it reads as what it is.
            + (
                f'<tspan x="{x:.1f}" dy="{_sz(12, geo)}" font-size="{_sz(10, geo)}">'
                f'{_esc(labelled[moment][1])}</tspan>'
                if labelled[moment][1] else ""
            )
            + "</text>"
        )
    out.append("</g>")
    out.append(
        f'<line x1="{geo.left}" y1="{geo.top + plot_h}" x2="{geo.left + plot_w}" '
        f'y2="{geo.top + plot_h}" stroke="{INK}" stroke-width="{_sz(1.1, geo)}"/>'
    )

    # One colour assignment for the whole figure, made once.
    #
    # The hue comes from what a series MEASURES, so every temperature in a
    # figure is a shade of one colour and a reader sees they are the same kind
    # of thing before reading a label. Which shade depends on how many share
    # the quantity; alone, a channel always takes its family's anchor. A colour
    # the caller asked for is never moved.
    colour_notes: list[str] = []
    asked = {s.name: s.colour for s in series if s.colour}
    asked.update({k: v for k, v in (series_colours or {}).items()})
    assigned, colour_notes = assign_colours(
        [(s.name, s.unit) for s in series], declared=asked, preferred=preferred_colours
    )
    colour_of = dict(enumerate(assigned))
    # A model wears the colour of the thing it models, dashed.
    #
    # It used to wear one reserved red, which said "this is a model" and lost
    # WHICH model: two channels and their two models drew both models in the
    # same colour, and the reader could not pair them. Hue carries the quantity
    # now, and the dash and the word "(model)" carry the provenance — which is
    # two carriers for it, where colour was only ever one.
    # Measured only: a model usually carries the same name as the thing it
    # models, and over every series it looked ITSELF up and paired with nothing.
    by_series_name = {s.name: i for i, s in enumerate(series) if not s.modelled}
    for i, s in enumerate(series):
        if s.modelled and s.against and s.against in by_series_name and not s.colour:
            colour_of[i] = colour_of[by_series_name[s.against]]

    # The divergence band, drawn under the lines so it never hides one.
    by_name = {s.name: s for s in series}
    for band_index, s in enumerate(series):
        if not (s.modelled and s.against and s.against in by_name):
            continue
        other = by_name[s.against]
        paired = [
            (t, a, b)
            for (t, a), (_, b) in zip(other.points, s.points, strict=False)
            if a is not None and b is not None
        ]
        if len(paired) < 2:
            continue
        top = " ".join(f"{x_of(t):.1f},{y_of(a, other.unit):.1f}" for t, a, _ in paired)
        bottom = " ".join(
            f"{x_of(t):.1f},{y_of(b, s.unit):.1f}" for t, _, b in reversed(paired)
        )
        out.append(
            f'<polygon points="{top} {bottom}" '
            f'fill="{colour_of[band_index]}" opacity="0.13"/>'
        )

    # The marks.
    #
    # One kind drawn three ways. A line, a bar and a scatter over a time axis
    # are the same chart with a different mark, not three chart kinds, which is
    # why this is a property of the series rather than of the spec.
    #
    # Whichever mark is chosen, a gap stays a gap: joining across a missing
    # reading draws one nobody took, and a bar or a dot for it would assert a
    # measurement at an instant that has none.
    baseline = geo.top + plot_h
    # Everything that follows is clipped to the plot.
    #
    # A reading outside the axis range has to go SOMEWHERE, and without this
    # it goes wherever the arithmetic puts it: across the footer, over the
    # labels, off the canvas. That became visible the moment a held axis
    # started working — a sweep holds the range it opened on, and the record
    # it travels through is wider than that range, so the trace left the box
    # and kept going. It was always possible; holding just made it certain.
    #
    # Clipping is also the honest rendering. The axis says what it covers; a
    # line that leaves it has left it, and drawing it outside the frame
    # asserts a position on an axis that does not extend that far.
    # A path rather than a rect: a `<rect>` in here is indistinguishable from
    # a drawn bar to anything reading the document, and the figure's own
    # guards count bars.
    # A clip id UNIQUE to this figure.
    #
    # It was the literal "axk-plot" in every figure the renderer produced. An
    # SVG id is document-scoped, not element-scoped, so two figures on one page
    # both define `clipPath id="axk-plot"` and `url(#axk-plot)` resolves to
    # whichever appears FIRST in the document. Every figure after the first is
    # then clipped to the first one's plot rectangle.
    #
    # Filling a chart to the screen is exactly that situation: the full-screen
    # copy is a second SVG beside the page one. A 1920x820 figure was being
    # clipped to a 1720x480 figure's box, which cut everything below 48% of its
    # height and beyond 87% of its width. Ben reported it as the bottom being
    # cut off, as half the graph being chopped, and as the x-axis not reaching
    # the right edge. One cause, three descriptions, and it survived because
    # each SVG is correct on its own and only the pair is wrong.
    #
    # Derived from the geometry rather than a counter, so the same figure
    # rendered twice is byte-identical and an export stays reproducible.
    clip_id = "axk-plot-%08x" % (
        zlib.crc32(
            f"{geo.left},{geo.top},{plot_w},{plot_h}".encode()
        )
        & 0xFFFFFFFF
    )
    out.append(
        f'<clipPath id="{clip_id}"><path d="M{geo.left},{geo.top} '
        f'h{plot_w} v{plot_h} h-{plot_w} Z"/></clipPath>'
    )
    out.append(f'<g class="axk-marks" clip-path="url(#{clip_id})">')
    # The dash is a CONTRAST, so it is drawn only where there is something to
    # contrast with. A figure of nothing but models had every line dashed —
    # which distinguishes none of them from each other, and spends the
    # legibility of a broken line to say something no line on the plot
    # denies. Ben: "the dotted line for the plots doesn't look as good as it
    # could look without the dotted line."
    #
    # Provenance is not lost when the dash goes: "(model)" still sits in
    # every one of those labels, and it is the carrier that survives being
    # printed small, photocopied, or read by somebody who cannot see the
    # difference between a dashed and a solid stroke.
    measured_present = any(not s.modelled for s in series)
    out_dash = ' stroke-dasharray="5 3.5"'
    for index, s in enumerate(series):
        colour = colour_of[index]
        dash = out_dash if (s.modelled and measured_present) else ""
        mark = getattr(s, "mark", "line") or "line"

        if mark == "scatter":
            for moment, value in s.points:
                if not plottable(value, s.unit):
                    continue
                out.append(
                    f'<circle cx="{x_of(moment):.1f}" cy="{y_of(value, s.unit):.1f}" '
                    f'r="{_sz(2.4, geo)}" fill="{colour}" '
                    f'fill-opacity="{0.55 if s.modelled else 0.85}"/>'
                )
            continue

        if mark == "bar":
            drawn = [(x_of(m), y_of(v, s.unit))
                     for m, v in s.points if plottable(v, s.unit)]
            # Width from the median spacing, so a ragged series does not
            # produce bars that overlap or hide gaps behind a wide one.
            scale = getattr(geo, "scale", 1.0)
            gaps = sorted(b[0] - a[0] for a, b in zip(drawn, drawn[1:], strict=False))
            step = gaps[len(gaps) // 2] if gaps else 6.0 * scale
            w = max(1.0 * scale, step * 0.72 / max(1, len(series)))
            for i, (x, y) in enumerate(drawn):
                off = (index - (len(series) - 1) / 2) * w
                out.append(
                    f'<rect x="{x + off - w / 2:.1f}" y="{min(y, baseline):.1f}" '
                    f'width="{w:.2f}" height="{abs(baseline - y):.1f}" '
                    f'fill="{colour}" fill-opacity="{0.55 if s.modelled else 0.85}"/>'
                )
            continue

        # A gap breaks the path. Joining across one draws a reading nobody
        # took.
        #
        # A run of ONE is a mark, not nothing. Two ways that used to disappear:
        # an isolated reading between two gaps was dropped outright, because
        # only runs of two or more were emitted; and a series whose whole span
        # falls inside one bucket of a much wider window drew a 2.6-pixel speck
        # on a 920-pixel canvas. Both read as "this series is not here", which
        # is the one thing it is not.
        def _emit(points: list[str]) -> None:
            if len(points) > 1:
                out.append(
                    f'<polyline points="{" ".join(points)}" fill="none" '
                    f'stroke="{colour}" stroke-width="{_sz(1.6, geo)}" '
                    f'stroke-linejoin="round" stroke-linecap="round"{dash}/>'
                )
            elif points:
                x, y = points[0].split(",")
                out.append(
                    f'<circle cx="{x}" cy="{y}" r="{_sz(3.4, geo)}" fill="{colour}"'
                    + (' fill-opacity="0.75"' if s.modelled else "")
                    + "/>"
                )

        run: list[str] = []
        for moment, value in s.points:
            if not plottable(value, s.unit):
                _emit(run)
                run = []
                continue
            run.append(f"{x_of(moment):.1f},{y_of(value, s.unit):.1f}")
        _emit(run)
    out.append("</g>")

    # What the figure drew, for anything that wants to annotate it: a
    # crosshair, a readout, an export. Name, unit and the colour this figure
    # gave it — the colour is assigned HERE, and a reader's crosshair showing a
    # different one from the line it is tracking is worse than no crosshair.
    #
    # Metadata only: no geometry, no values. The values are the caller's, and
    # repeating two thousand points per series inside the picture would double
    # the size of every figure to serve one hover.
    for index, s in enumerate(series):
        out.append(
            f'<g class="axk-series" data-series="{_esc(s.name)}" '
            f'data-unit="{_esc(s.unit)}" data-colour="{colour_of.get(index, INK)}"'
            + (' data-modelled="1"' if s.modelled else "")
            + "></g>"
        )

    # Direct labels, nudged apart. A legend makes the reader look away from
    # the line, hold a colour in memory, and look back.
    anchored = []
    for index, s in enumerate(series):
        live = [(t, v) for t, v in s.points if plottable(v, s.unit)]
        if live:
            anchored.append((y_of(live[-1][1], s.unit), index, s, live[-1]))

    minimum_gap = plot_h * geo.label_gap
    if held_extent:
        # A HELD axis means a sweep is running: the caller pinned the range so
        # the picture would stay still while the data moved through it. A
        # label that tracks its line's last value does not stay still — it
        # bobs, and a reader following one has to chase it up and down the
        # plot instead of reading the figure.
        #
        # So while the axis is held, the labels are too: one fixed stack
        # centred on the plot, in the order the series were given, which is
        # the order they appear in the picker and does not change as values
        # cross. Only the leader lines move, and a leader line is a thing the
        # eye can follow WITHOUT tracking it.
        anchored.sort(key=lambda item: item[1])
        middle = geo.top + plot_h / 2
        # A slot per SERIES, not per series-that-has-a-reading-right-now.
        # Sizing the stack to whoever happened to be drawable this frame was
        # still movement: a series with no reading in the current window
        # drops out, the stack re-centres on one fewer, and every remaining
        # label steps. The reader sees exactly what they were promised would
        # stop — labels drifting — for a reason invisible on screen.
        #
        # NOT `span`: that name belongs to the figure's time span, which the
        # `x_of` closure above reads. Rebinding it here silently moved every
        # point in the plot to x = left.
        stack_height = minimum_gap * max(len(series) - 1, 0)
        top_slot = middle - stack_height / 2
        placed = [top_slot + index * minimum_gap for _y, index, _s, _last in anchored]
    else:
        # A still figure puts each label beside its own line, which is the
        # whole point of labelling directly rather than in a legend.
        #
        # Laid out in the order the lines END, so a label never sits below a
        # label whose line is above it. Nudging each one down as it arrived
        # inverted them: the model ran above the measurement and was labelled
        # beneath it, which makes a reader follow a leader line to believe
        # the figure.
        anchored.sort(key=lambda item: item[0])
        placed = []
        for wanted, *_ in anchored:
            y = wanted if not placed else max(wanted, placed[-1] + minimum_gap)
            placed.append(y)
    # Pulled back inside the plot if the stack ran past the bottom, still in
    # order — and off the top, which a centred stack of many series can do on
    # a short plot.
    #
    # Both corrections read `plot_h`, which grows when the footer gains a
    # line. A held stack must not: the footer gains a line exactly when a
    # series stops being drawable, so correcting against it would reintroduce
    # the drift by the back door. Centred on the plot, a stack that overflows
    # is already symmetric about the middle, so there is nothing to correct.
    if not held_extent:
        overflow = placed[-1] - (geo.top + plot_h) if placed else 0
        if overflow > 0:
            placed = [y - overflow for y in placed]

    for slot, (_wanted, index, s, last) in enumerate(anchored):
        colour = colour_of[index]
        last_t, last_v = last
        y = placed[slot]
        x = x_of(last_t) + 9 + right_gutter
        out.append(
            f'<line x1="{x_of(last_t):.1f}" y1="{y_of(last_v, s.unit):.1f}" '
            f'x2="{x - 2:.1f}" y2="{y:.1f}" stroke="{colour}" '
            f'stroke-width="{_sz(1, geo)}" opacity="0.5"/>'
        )
        suffix = " (model)" if s.modelled else ""
        # The SAME scale the axis uses for this unit. Scaling the label
        # independently produced an axis in MW beside a label in W, which
        # is the kind of small inconsistency that makes a reader stop
        # trusting the rest of the figure.
        own_divisor, own_unit = shown[s.unit]
        # Rounded to the axis's own resolution. `format_number` shows every
        # digit a float carries, which is right in a table where the column
        # is the data — and wrong on a label, where 237.676725 degC claims a
        # thermocouple resolved microkelvin.
        low, high = units[s.unit]
        places = _places_for((high - low) / own_divisor)
        # An undeclared unit is SAID, not skipped. `' ' + own_unit if
        # own_unit else ''` rendered the value alone, which is the figure
        # asserting a dimensionless quantity.
        drawn = format_number(round(last_v / own_divisor, places))
        reading = (
            f"{drawn} {own_unit}" if declared(own_unit)
            else f"{drawn} ({UNDECLARED_BRIEF})"
        )
        # Name and value on ONE line, the value as a tspan so it flows
        # without this having to measure text. Stacked, they sat as close
        # to the next series' name as to their own, and the reader had to
        # work out which line a number belonged to — the one thing a
        # direct label exists to remove.
        out.append(
            f'<text x="{x:.1f}" y="{y + 3.5 * geo.scale:.1f}" '
            f'font-size="{_sz(12, geo)}" fill="{colour}" '
            f'font-family="{LABEL_FONT}" font-weight="600">'
            f'{_esc(s.name)}{_esc(suffix)}'
            # `dx`, not spaces: SVG collapses leading whitespace, which
            # ran the value straight into the name as "ROM (model)238".
            f'<tspan fill="{MUTED}" font-weight="400" dx="{4.5 * geo.scale:.1f}" '
            f'font-size="{_sz(10.5, geo)}">{_esc(reading)}</tspan></text>'
        )

    notes: list[str] = []
    if plan.unverified:
        # Drawn, and said. A figure that shows them without a word implies the
        # axis was checked for them; one that drops them shows a third of what
        # was asked for. Neither is the truth, which is that a reader asked to
        # compare these and nobody wrote down what two of them are measured in.
        who = ", ".join(plan.unverified)
        notes.append(
            f"{who} declare no unit and are drawn against "
            f"{unit_label(leading, brief=True)}, unverified. A channel map is "
            "where a unit is written."
        )
    if left_out:
        # Name the SERIES, not just the unit. "not shown: cm" describes the
        # mechanism; a reader who picked four channels and sees two lines
        # needs to know which two are missing and what to do about it.
        who = ", ".join(dropped_names) or ", ".join(left_out)
        if any(not u.strip() for u in left_out):
            notes.append(
                f"not drawn: {who} — no unit is declared for "
                f"{'it' if len(dropped_names) == 1 else 'them'}, so "
                f"{'it' if len(dropped_names) == 1 else 'they'} cannot share "
                f"an axis with {unit_label(leading, brief=True)}. "
                "A channel map is where that is written."
            )
        else:
            notes.append(
                f"not drawn: {who} ({', '.join(left_out)}) — a third axis "
                "would be read as one of the other two"
                if secondary_unit else
                f"not drawn: {who} ({', '.join(left_out)}) — a second axis "
                f"would be read as the {unit_label(leading, brief=True)} one"
            )
    notes.extend(colour_notes)
    if unplottable:
        # Said out loud, because a log axis silently discarding readings is the
        # quietest way for a figure to be wrong: the line simply looks shorter.
        notes.append(
            f"{unplottable} reading(s) at or below zero could not be placed on a "
            "logarithmic axis and are drawn as gaps"
        )
    # The footer: the axis, then a blank line, then what the figure could not
    # do, then the provenance. Previously stacked at 1.1 line leading directly
    # under a tick label that might itself carry a date, so a two-note figure
    # ran its notes through its own axis and its provenance through those.
    dated = any(labelled[m][1] for m in ticks)
    footer_top = geo.top + plot_h + small * (2.6 if dated else 1.45) + small * 1.25
    for i, note in enumerate(notes):
        out.append(
            f'<text x="{geo.left}" y="{footer_top + small * i * 1.25:.1f}" '
            f'font-size="{_sz(10, geo)}" fill="{MUTED}" '
            f'font-family="{LABEL_FONT}" font-style="italic">'
            f'{_esc(note)}</text>'
        )

    footer_bottom = footer_top + small * max(len(notes) - 1, 0) * 1.25
    if provenance:
        footer_bottom += small * (1.55 if notes else 0.3)
        out.append(
            f'<text x="{geo.left}" y="{footer_bottom:.1f}" font-size="{_sz(10.5, geo)}" '
            f'fill="{MUTED}" font-family="{LABEL_FONT}">{_esc(provenance)}</text>'
        )
    out.append("</svg>")

    # Grow the canvas to hold the footer rather than running the footer off
    # it. The margin is sized before the notes exist — they depend on what the
    # figure turned out to be able to draw — so the honest order is to lay it
    # out and then make room. The PLOT is untouched: a figure that shrank its
    # own chart to fit its own apology would be the wrong trade.
    height = max(geo.height, int(footer_bottom + small * 0.8))
    if height != geo.height:
        out[0] = out[0].replace(
            f'height="{geo.height}" viewBox="0 0 {geo.width} {geo.height}"',
            f'height="{height}" viewBox="0 0 {geo.width} {height}"', 1)
        out[1] = out[1].replace(f'height="{geo.height}"', f'height="{height}"', 1)
    # The plot rectangle, on the root element, so anything holding this SVG can
    # map a pointer position to an instant without asking how the figure was
    # laid out. Written LAST because the left margin and the right gutter are
    # only settled once the labels have been measured.
    #
    # One attribute rather than a second endpoint: a crosshair that reads a
    # different geometry from the one the figure was drawn with is a crosshair
    # that points at the wrong place, and it would be wrong only sometimes.
    out[0] = out[0].replace(
        'font-family="',
        f'data-plot="{geo.left:.1f} {geo.top:.1f} {plot_w:.1f} {plot_h:.1f}" font-family="',
        1,
    )
    return "\n".join(out)


def _empty(geo: Geometry, title: str, message: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{geo.width}" '
        f'height="{geo.height}" font-family="{FONT}">'
        f'<rect width="{geo.width}" height="{geo.height}" fill="{PAPER}"/>'
        f'<text x="{geo.left}" y="30" font-size="{_sz(19, geo)}" fill="{INK}">{_esc(title)}</text>'
        f'<text x="{geo.left}" y="{geo.height // 2}" font-size="{_sz(13, geo)}" '
        f'fill="{MUTED}">{_esc(message)}</text></svg>'
    )


__all__ = ["FONT", "HOUSE_COLOURS", "Geometry", "Series",
           "render_svg", "scale_for"]
