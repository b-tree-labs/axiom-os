# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""When a name carries a unit.

A channel called ``corrected_cm`` names its unit. Six channels on one feed were
called ``corrected_cm``, ``measured_cm``, ``predicted_cm``, ``rom_matched_cm``
and two ``_interp`` variants, and exactly two of the six declared ``cm`` in the
field meant for it. The other four served bare numbers while their own names
said what they were.

That is two separate faults wearing one costume, and they need opposite
treatments:

**The name is not a unit.** A name is what somebody else's instrument calls a
thing, and it is never rewritten — a rename forks the data, which this
programme has already paid for once. Inferring a unit from it and serving the
result as a fact is exactly the failure the unit rule exists to stop: a guess
that reads as a measurement. So nothing here writes a unit anywhere.

**But a name that carries one is EVIDENCE, and evidence is worth acting on.**
Two things follow, and both are cheap:

1. Where the name names a unit and the declaration omits it, that is a specific,
   actionable finding — not "this channel has no unit" but "this channel's own
   name says cm and its declaration does not". The fix is a line in a channel
   map, and the finding says which line.

2. Where the name names a unit and the declaration DISAGREES, that is a
   conflict, and a conflict is more serious than an absence. ``depth_mm``
   declared in metres is a figure that is wrong by a thousand and looks fine.

And one thing for the surfaces:

3. Where the name names a unit and the declaration AGREES, saying it twice is
   noise. ``corrected_cm · cm`` tells a reader nothing the name did not. Say it
   once.

Domain-agnostic: these are SI units and an underscore, which mean the same
thing wherever somebody measures something.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .units import declared

__all__ = [
    "NameUnit",
    "SUFFIX_UNITS",
    "agreement",
    "unit_in_name",
]

#: Unit tokens a name may end with, and the unit each one means.
#:
#: Suffixes only, and only ones that are unambiguous as a whole word at the end
#: of a name. ``power_w`` means watts; ``flow`` does not mean farads because it
#: ends in no token at all, and ``new`` is not watts because the token must be
#: preceded by a separator. Anything ambiguous is deliberately absent: a table
#: that guesses is worse than one that stays quiet, because a wrong unit is a
#: wrong number and an absent one is at least visibly absent.
SUFFIX_UNITS: dict[str, str] = {
    # length
    "mm": "mm", "cm": "cm", "m": "m", "km": "km", "um": "um", "in": "in", "ft": "ft",
    # temperature
    "c": "degC", "degc": "degC", "f": "degF", "degf": "degF", "k": "K",
    "celsius": "degC", "kelvin": "K",
    # power and energy
    "w": "W", "kw": "kW", "mw": "MW", "j": "J", "kj": "kJ", "wh": "Wh", "kwh": "kWh",
    # pressure
    "pa": "Pa", "kpa": "kPa", "mpa": "MPa", "bar": "bar", "psi": "psi", "torr": "torr",
    # flow and volume
    "lpm": "L/min", "gpm": "gpm", "cfm": "cfm", "l": "L", "ml": "mL",
    # electrical
    "v": "V", "mv": "mV", "kv": "kV", "a": "A", "ma": "mA", "hz": "Hz",
    # time
    "s": "s", "ms": "ms", "sec": "s", "min": "min", "hr": "h", "h": "h",
    # dimensionless and ratios
    "pct": "percent", "percent": "percent", "ppm": "ppm", "ppb": "ppb",
    "pcm": "pcm",
    # radiation
    "bq": "Bq", "ci": "Ci", "sv": "Sv", "msv": "mSv", "gy": "Gy", "cps": "cps",
}

#: Words a name may end with AFTER its unit, which the unit is still about.
#: `corrected_cm_interp` is centimetres interpolated, not a channel with no
#: unit — and four of the six channels that started this were `_interp` or
#: similar. Missing them would have missed most of the case.
QUALIFIERS = frozenset({
    "interp", "interpolated", "raw", "avg", "average", "mean", "min", "max",
    "sum", "total", "delta", "diff", "std", "sigma", "rms", "pred", "predicted",
    "measured", "corrected", "matched", "smoothed", "filtered", "est",
    "estimated", "nominal", "setpoint", "target", "actual",
})

_SPLIT = re.compile(r"[_\-. ]+")


@dataclass(frozen=True)
class NameUnit:
    """A unit a name appears to carry."""

    #: the unit, spelled the way a declaration would spell it
    unit: str
    #: the token in the name it came from, as written
    token: str


def unit_in_name(channel: str) -> NameUnit | None:
    """The unit this channel's NAME carries, or ``None``.

    Never a declaration and never written anywhere. It is evidence that a
    declaration is missing or wrong, and the value of it is that it says which.
    """
    parts = [p for p in _SPLIT.split(str(channel or "")) if p]
    if len(parts) < 2:
        # A bare `cm` names no channel; a unit is a suffix ON something.
        return None
    for part in reversed(parts[1:]):
        lowered = part.lower()
        if lowered in QUALIFIERS:
            continue
        unit = SUFFIX_UNITS.get(lowered)
        return NameUnit(unit=unit, token=part) if unit else None
    return None


@dataclass(frozen=True)
class Agreement:
    """What the name and the declaration say between them."""

    #: the unit to show, which is the DECLARED one whenever there is one
    unit: str
    #: the name carries a unit and the declaration omits it. A finding with a
    #: fix attached: a line in a channel map, and this says which line.
    undeclared_but_named: str = ""
    #: the name and the declaration disagree. Worse than an absence: a figure
    #: that is wrong by a factor and looks fine.
    conflict: str = ""
    #: both say the same thing, so a surface should say it ONCE
    redundant: bool = False


def agreement(channel: str, unit: object) -> Agreement:
    """Set a channel's name against its declared unit.

    The declaration always wins the *value*: a site that says a thing knows its
    own instrument, and a name is a label somebody typed. What the name changes
    is what a surface should SAY about the pair.
    """
    named = unit_in_name(channel)
    spelled = str(unit or "").strip()
    if not declared(spelled):
        return Agreement(
            unit="",
            undeclared_but_named=named.unit if named else "",
        )
    if named is None:
        return Agreement(unit=spelled)
    if named.unit.lower() == spelled.lower():
        return Agreement(unit=spelled, redundant=True)
    return Agreement(
        unit=spelled,
        conflict=f"the name says {named.unit}, the declaration says {spelled}",
    )
