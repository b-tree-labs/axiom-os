# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Which colour a series is drawn in, and why.

A colour that means one thing in one figure and another in the next is
decoration. Two promises make it a code instead:

**The same channel is the same colour everywhere.** Not "the same colour in
this figure", which walking a palette by position already gave — the same
colour next week, in a different figure, next to different channels. That
cannot come from position, because position moves when a channel is added,
dropped, or simply falls outside a narrower window. It comes from the channel's
own name.

**Related quantities look related.** All the temperatures in one figure are
shades of one hue, and the reader sees they are the same kind of thing before
reading a single label. The hue is chosen by the UNIT, which is a physical fact
and not a domain noun: this module never learns what a channel measures, only
what it is measured in.

## Three tiers, highest wins

1. **Declared** on the chart document, which travels with the figure.
2. **Preferred** by the person — "always draw this channel in blue" — which
   persists across every figure they draw.
3. **Derived** from the unit and the channel's name, which needs no
   coordination and is still the same answer every time.

## About the hues

Four are conventions a reader already holds, and the module states which:

- **temperature is warm.** Hot is red. There is no more widely held colour
  convention in any technical field.
- **flow is blue.** Water is blue on every schematic ever drawn.
- **pressure is green.** The process-and-instrumentation habit.
- **ionising radiation is magenta.** ISO 361 and ANSI Z535 both put the
  trefoil in magenta.

The rest are ARBITRARY but stable. That is said plainly rather than dressed up:
there is no received colour for electrical potential or for power, and claiming
one would be inventing a convention and then citing it. What those families
still buy is grouping — every channel of one quantity looks like the others —
and that is the part worth having.

A series whose unit was never declared is drawn desaturated. The figure looks
less certain because it is, which is the same thing the axis label already says
in words.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

__all__ = [
    "FAMILIES",
    "Family",
    "family_for",
    "derived_colour",
    "assign_colours",
    "is_colour",
    "normalise_colour",
    "NAMED_COLOURS",
]


@dataclass(frozen=True)
class Family:
    """A band of colour space that one physical quantity is drawn in."""

    #: Centre of the hue band, in CIE Lab degrees.
    hue: float
    #: How wide the band is. Wider separates more channels; too wide and two
    #: families meet.
    span: float
    #: Lightness range the shades are drawn from, and the band is DIFFERENT for
    #: each family on purpose.
    #:
    #: Two of the conventions are red and green, which is precisely the pair
    #: about eight percent of men cannot separate by hue: a temperature and a
    #: pressure measured 6.9 apart under deuteranopia, which is nothing. Hue
    #: cannot carry that distinction and keep the convention, so lightness
    #: carries it — lightness is what a dichromacy leaves alone. It is also
    #: where the shades WITHIN a family come from, which is why each band is
    #: wide enough for four.
    lightness: tuple[float, float]
    #: Chroma. Lower reads as more restrained; a family with no unit declared
    #: sits near zero so the figure shows its own uncertainty.
    chroma: float
    #: Whether the hue is a convention a reader already holds, or simply a
    #: stable allocation. Stated because the difference matters.
    conventional: bool
    why: str


#: Hue bands, kept apart so two families never meet. Checked by a test.
FAMILIES: dict[str, Family] = {
    "temperature": Family(
        hue=35, span=24, lightness=(22, 54), chroma=44, conventional=True,
        why="hot is red; the most widely held colour convention there is",
    ),
    "pressure": Family(
        hue=140, span=24, lightness=(40, 72), chroma=40, conventional=True,
        why="the process-and-instrumentation habit",
    ),
    "flow": Family(
        hue=250, span=24, lightness=(40, 72), chroma=44, conventional=True,
        why="water is blue on every schematic ever drawn",
    ),
    "radiation": Family(
        hue=340, span=24, lightness=(22, 54), chroma=44, conventional=True,
        why="ISO 361 and ANSI Z535 put the trefoil in magenta",
    ),
    "power": Family(
        hue=280, span=24, lightness=(34, 66), chroma=44, conventional=False,
        why="no received convention; allocated so power channels group",
    ),
    "electrical": Family(
        hue=65, span=24, lightness=(22, 54), chroma=44, conventional=False,
        why="no received convention; allocated so electrical channels group",
    ),
    "length": Family(
        hue=170, span=24, lightness=(28, 60), chroma=44, conventional=False,
        why="no received convention; allocated so positions group",
    ),
    "fraction": Family(
        hue=300, span=20, lightness=(34, 66), chroma=22, conventional=False,
        why="a ratio is not a quantity of a thing, so it is drawn quietly",
    ),
    #: No unit was declared. Grey, because the figure should look as uncertain
    #: as it is — and DARKER than any family anchor, because under a dichromacy
    #: every hue collapses towards grey and a green line was landing 3.7 from
    #: this one. Lightness is what survives, so lightness is what separates it.
    "undeclared": Family(
        hue=0, span=26, lightness=(40, 72), chroma=0, conventional=False,
        why="the unit was never declared, and the colour says so",
    ),
}


def _units(*names: str) -> frozenset[str]:
    return frozenset(n.lower() for n in names)


#: A unit, as written, to the quantity it measures. Units are physical facts,
#: not domain nouns: this table would read the same in a chemistry lab, a wind
#: tunnel or a brewery.
UNIT_FAMILY: dict[frozenset[str], str] = {
    _units("degc", "degf", "degk", "°c", "°f", "celsius", "fahrenheit",
           "kelvin", "k"): "temperature",
    _units("pa", "kpa", "mpa", "hpa", "bar", "mbar", "psi", "psia", "psig",
           "torr", "atm", "mmhg", "inhg"): "pressure",
    _units("m3/s", "m3/h", "m^3/s", "l/s", "l/min", "lpm", "gpm", "gph",
           "kg/s", "kg/h", "g/s", "sccm", "slpm", "cfm", "m/s",
           "ft/s"): "flow",
    _units("bq", "kbq", "mbq", "gbq", "ci", "mci", "uci", "sv", "msv", "usv",
           "sv/h", "msv/h", "gy", "mgy", "rad", "rem", "mrem", "r/h", "mr/h",
           "cps", "cpm", "n/cm2/s", "n/cm^2/s", "nv"): "radiation",
    _units("w", "kw", "mw", "gw", "mww", "hp", "j", "kj", "mj", "wh", "kwh",
           "mwh", "ev", "kev", "mev", "btu", "cal", "kcal"): "power",
    _units("v", "mv", "kv", "uv", "a", "ma", "ua", "ka", "ohm", "kohm",
           "mohm", "f", "uf", "nf", "pf", "h", "mh", "hz", "khz",
           "mhz"): "electrical",
    _units("m", "mm", "cm", "km", "um", "nm", "in", "ft", "mil", "deg",
           "rad", "step", "steps", "turn", "turns",
           "console_units", "console_unit"): "length",
    _units("%", "pct", "percent", "ratio", "fraction", "ppm", "ppb", "ppt",
           "pcm", "dpm"): "fraction",
}

_BY_UNIT: dict[str, str] = {
    unit: family for units, family in UNIT_FAMILY.items() for unit in units
}

#: Names a person may reasonably type for a colour. Anything else must be an
#: ``#rrggbb``, because a colour nobody can name is one nobody can check.
NAMED_COLOURS: dict[str, str] = {
    "blue": "#0072b2",
    "green": "#009e73",
    "red": "#c0392b",
    "orange": "#d55e00",
    "purple": "#5b4a8a",
    "magenta": "#a34e78",
    "pink": "#cc79a7",
    "teal": "#1f7a7a",
    "olive": "#4d4119",
    "brown": "#6b4423",
    "slate": "#6991a7",
    "grey": "#6b7280",
    "gray": "#6b7280",
    "black": "#1a1a1a",
}


def family_for(unit: object) -> str:
    """Which quantity family a unit belongs to.

    An SI prefix is stripped before looking up, so ``kW`` and ``MW`` land where
    ``W`` does — a prefix changes the size of a number, never what it measures.
    """
    text = str(unit or "").strip()
    if not text:
        return "undeclared"
    lowered = text.lower()
    if lowered in _BY_UNIT:
        return _BY_UNIT[lowered]
    for prefix in ("da", "y", "z", "e", "p", "t", "g", "m", "k", "h", "d",
                   "c", "n", "u", "µ", "f", "a"):
        if lowered.startswith(prefix) and lowered[len(prefix):] in _BY_UNIT:
            return _BY_UNIT[lowered[len(prefix):]]
    return "undeclared"


# ---------------------------------------------------------------------------
# Colour space. Enough of it to place a colour inside a band and get it back
# out as something a browser will draw.
# ---------------------------------------------------------------------------


def _to_srgb(c: float) -> float:
    return 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055


def _to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def lab_to_rgb(lightness: float, a: float, b: float) -> tuple[float, float, float] | None:
    """``(r, g, b)`` in 0..1, or ``None`` when the colour is outside sRGB."""
    fy = (lightness + 16) / 116
    fx, fz = fy + a / 500, fy - b / 200

    def g(t: float) -> float:
        return t**3 if t**3 > 0.008856 else (t - 16 / 116) / 7.787

    x, y, z = g(fx) * 0.95047, g(fy), g(fz) * 1.08883
    channels = (
        x * 3.2406 + y * -1.5372 + z * -0.4986,
        x * -0.9689 + y * 1.8758 + z * 0.0415,
        x * 0.0557 + y * -0.2040 + z * 1.0570,
    )
    if any(c < -1e-6 or c > 1 + 1e-6 for c in channels):
        return None
    return tuple(_to_srgb(min(1.0, max(0.0, c))) for c in channels)  # type: ignore[return-value]


def rgb_to_lab(channels: tuple[float, float, float]) -> tuple[float, float, float]:
    r, g, b = (_to_linear(c) for c in channels)
    x = r * 0.4124 + g * 0.3576 + b * 0.1805
    y = r * 0.2126 + g * 0.7152 + b * 0.0722
    z = r * 0.0193 + g * 0.1192 + b * 0.9505

    def f(t: float) -> float:
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116

    fx, fy, fz = f(x / 0.95047), f(y), f(z / 1.08883)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def _hex(channels: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{round(c * 255):02x}" for c in channels)


def _channels(colour: str) -> tuple[float, float, float]:
    text = colour.lstrip("#")
    return tuple(int(text[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def contrast_on_white(colour: str) -> float:
    """The WCAG ratio. A line on paper wants at least 3.0."""

    def luminance(chans):
        r, g, b = (_to_linear(c) for c in chans)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    return (1.0 + 0.05) / (luminance(_channels(colour)) + 0.05)


def difference(one: str, two: str) -> float:
    """CIE76 ``dE`` between two colours."""
    return math.dist(rgb_to_lab(_channels(one)), rgb_to_lab(_channels(two)))


def _place(family: Family, across: float, along: float) -> str:
    """A colour inside the band. ``across`` and ``along`` are each 0..1.

    Chroma eases down if the colour would fall outside sRGB or come out too
    pale to draw as a line, so a band never yields something unusable.
    """
    hue = math.radians(family.hue + (across - 0.5) * family.span)
    low, high = family.lightness
    lightness = low + along * (high - low)
    for shrink in (1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.4):
        chroma = family.chroma * shrink
        rgb = lab_to_rgb(lightness, chroma * math.cos(hue), chroma * math.sin(hue))
        if rgb is None:
            continue
        colour = _hex(rgb)
        if contrast_on_white(colour) >= 3.0:
            return colour
    # Nothing in the band carried; darken until something does rather than
    # hand back a line the reader cannot see.
    for step in range(1, 40):
        rgb = lab_to_rgb(max(18.0, lightness - step * 2),
                         family.chroma * 0.6 * math.cos(hue),
                         family.chroma * 0.6 * math.sin(hue))
        if rgb is not None and contrast_on_white(_hex(rgb)) >= 3.0:
            return _hex(rgb)
    return "#1a1a1a"


#: How far apart two lines in one figure have to be before a reader pairs each
#: with its own label at a glance. Within one quantity the difference is mostly
#: lightness, which is the easiest difference there is to see and the one that
#: survives a colour-vision deficiency best, so this is lower than the floor
#: between two unrelated palette colours would be.
SEPARATION = 12.0


def _stable_rank(channel: str) -> bytes:
    """A sort key that is the same on every machine forever.

    ``hash()`` is randomised per process, so a figure drawn twice would have
    come out in different colours, which is the one thing this module exists to
    prevent.
    """
    return hashlib.sha256(channel.encode("utf-8")).digest()


def derived_colour(channel: str, unit: object = "") -> str:
    """The colour this channel is drawn in when it is the only one of its kind.

    The answer a person gets when they ask what colour a channel is, and the
    colour it actually takes in any figure where nothing else shares its
    quantity. A pure function of the name and the unit: two figures drawn a
    month apart on different machines agree without anything being shared.
    """
    family = FAMILIES[family_for(unit)]
    return _place(family, 0.5, 0.5)


def is_colour(value: object) -> bool:
    """Whether this is something the renderer can draw."""
    text = str(value or "").strip().lower()
    if text in NAMED_COLOURS:
        return True
    return (
        len(text) == 7
        and text.startswith("#")
        and all(c in "0123456789abcdef" for c in text[1:])
    )


def normalise_colour(value: object) -> str:
    """``"blue"`` or ``"#0072B2"`` as ``#0072b2``, or a refusal naming it."""
    text = str(value or "").strip().lower()
    if text in NAMED_COLOURS:
        return NAMED_COLOURS[text]
    if is_colour(text):
        return text
    known = ", ".join(sorted(NAMED_COLOURS))
    raise ValueError(
        f"{value!r} is not a colour; write it as #rrggbb, or use one of: {known}"
    )


def _spread(family: Family, count: int) -> list[tuple[float, float]]:
    """Where *count* members of one family sit inside its band.

    One member takes the anchor, so a figure of a single quantity is the same
    every time it is drawn. Several are spread evenly, mostly along lightness,
    because that is the difference a reader sees most easily and the one a
    dichromacy leaves alone.
    """
    if count <= 1:
        return [(0.5, 0.5)]
    return [
        (i / (count - 1), i / (count - 1))
        for i in range(count)
    ]


def assign_colours(
    series: list[tuple[str, object]],
    *,
    declared: dict[str, str] | None = None,
    preferred: dict[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    """``(colours, notes)`` for these ``(channel, unit)`` pairs.

    Declared beats preferred beats derived, and a colour that was asked for is
    never moved for any reason.

    What is guaranteed, and what is not, stated plainly because the difference
    is the whole point of the preference:

    - **The hue always holds.** A channel measured in degrees is warm in every
      figure it appears in, whatever else is drawn beside it.
    - **The shade holds while the company does.** Several channels of one
      quantity are spread across the family's band so each is legible, and
      which shade a channel takes depends on how many share its quantity in
      that figure. Alone, it always takes the anchor.
    - **A pinned channel never moves at all.** That is what pinning is for, and
      it is one command.

    Order comes from a stable hash of the name rather than the order the caller
    passed them in, so two callers listing the same channels differently still
    get the same figure.
    """
    declared = {k: normalise_colour(v) for k, v in (declared or {}).items()}
    preferred = {k: normalise_colour(v) for k, v in (preferred or {}).items()}

    colours: dict[int, str] = {}
    free: list[int] = []
    for i, (channel, _unit) in enumerate(series):
        asked = declared.get(channel) or preferred.get(channel)
        if asked:
            colours[i] = asked
        else:
            free.append(i)

    by_family: dict[str, list[int]] = {}
    for i in free:
        by_family.setdefault(family_for(series[i][1]), []).append(i)

    crowded: list[str] = []
    for name, members in by_family.items():
        family = FAMILIES[name]
        members.sort(key=lambda i: (_stable_rank(series[i][0]), series[i][0]))
        for (across, along), i in zip(_spread(family, len(members)), members, strict=True):
            colours[i] = _place(family, across, along)
        if len(members) > 1:
            drawn = [colours[i] for i in members]
            tight = min(
                difference(a, b)
                for a, b in ((drawn[x], drawn[y])
                             for x in range(len(drawn))
                             for y in range(x + 1, len(drawn)))
            )
            if tight < SEPARATION:
                crowded.append(name)

    notes: list[str] = []
    if crowded:
        which = ", ".join(sorted(crowded))
        notes.append(
            f"more {which} channels than the colour band separates; the names "
            "at the ends of the lines tell them apart"
        )
    return [colours[i] for i in range(len(series))], notes
