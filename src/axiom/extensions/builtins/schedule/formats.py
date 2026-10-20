# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Cadence-format codec — read (parse) and write (serialize) the predominant
schedule formats to/from a PULSE ``Cadence``.

PULSE speaks the common dialects so schedules import from, and export to, the
systems people already use:

- **cron** — 5/6-field expressions and ``@`` shortcuts (``@daily`` …). [shipped]
- **iso8601** — ISO-8601 durations / repeating intervals (``PT1H``, ``R/P1D``).
  [shipped]
- **rrule** — iCalendar RRULE (RFC 5545); the calendar-connector lingua franca,
  a native cadence kind for lossless round-trips. [shipped — dateutil.rrule]
- **systemd** — ``OnCalendar=`` expressions, for the host-unit sibling. [planned]

Conversions are exact where the formats overlap and raise ``FormatError`` where
they don't (e.g. a one-shot has no cron form; an irregular cron has no interval
form). Lossy mappings (an ``interval`` aligned onto wall-clock cron) are
documented at the call site.
"""

from __future__ import annotations

import re
from datetime import timedelta

from axiom.extensions.builtins.schedule.api import Cadence

DIALECTS = ("cron", "iso8601", "rrule", "systemd")
_SHIPPED = ("cron", "iso8601", "rrule")


class FormatError(ValueError):
    """A schedule string could not be parsed, or a cadence has no form in the
    requested dialect."""


# --- cron ---------------------------------------------------------------------

_CRON_SHORTCUTS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


def parse_cron(text: str) -> Cadence:
    t = text.strip()
    if t.startswith("@"):
        if t in _CRON_SHORTCUTS:
            return Cadence(kind="cron", cron=_CRON_SHORTCUTS[t])
        raise FormatError(f"unsupported cron shortcut: {t!r}")
    if len(t.split()) not in (5, 6):
        raise FormatError(f"cron expression needs 5 or 6 fields: {t!r}")
    return Cadence(kind="cron", cron=t)


def to_cron(cadence: Cadence) -> str:
    if cadence.kind == "cron":
        return cadence.cron
    if cadence.kind == "interval":
        secs = int(cadence.interval.total_seconds())
        if secs == 60:
            return "* * * * *"
        if secs == 3600:
            return "0 * * * *"
        if secs == 86400:
            return "0 0 * * *"
        if secs % 3600 == 0 and 24 % (secs // 3600) == 0:
            return f"0 */{secs // 3600} * * *"
        if secs % 60 == 0 and 60 % (secs // 60) == 0:
            return f"*/{secs // 60} * * * *"
        raise FormatError(
            f"interval of {secs}s is not exactly expressible as cron"
        )
    raise FormatError(f"{cadence.kind} cadence has no cron form")


# --- iso8601 ------------------------------------------------------------------

_ISO = re.compile(
    r"^(?:R\d*/)?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$"
)


def parse_iso8601(text: str) -> Cadence:
    m = _ISO.match(text.strip())
    if not m:
        raise FormatError(f"not an ISO-8601 duration/interval: {text!r}")
    w, d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    total = timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)
    if total.total_seconds() == 0:
        raise FormatError(f"ISO-8601 duration is zero: {text!r}")
    return Cadence(kind="interval", interval=total)


def to_iso8601(cadence: Cadence) -> str:
    if cadence.kind != "interval":
        raise FormatError(f"{cadence.kind} cadence has no ISO-8601 duration form")
    secs = int(cadence.interval.total_seconds())
    days, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    out = "P" + (f"{days}D" if days else "")
    tpart = (f"{h}H" if h else "") + (f"{m}M" if m else "") + (f"{s}S" if s else "")
    return out + ("T" + tpart if tpart else "") if (days or tpart) else "PT0S"


# --- rrule (iCalendar RFC 5545) -----------------------------------------------

def parse_rrule(text: str) -> Cadence:
    """Parse an iCalendar RRULE into a native rrule Cadence (lossless). The
    engine computes occurrences with ``dateutil.rrule``; bind the recurrence
    anchor by passing ``not_before`` (DTSTART) when registering."""
    t = text.strip()
    if "FREQ=" not in t.upper():
        raise FormatError(f"not an RRULE (no FREQ=): {text!r}")
    return Cadence(kind="rrule", rrule=t)


def to_rrule(cadence: Cadence) -> str:
    if cadence.kind == "rrule":
        r = cadence.rrule
        return r if r.upper().startswith("RRULE:") else f"RRULE:{r}"
    if cadence.kind == "interval":
        secs = int(cadence.interval.total_seconds())
        exact = {60: "FREQ=MINUTELY", 3600: "FREQ=HOURLY",
                 86400: "FREQ=DAILY", 604800: "FREQ=WEEKLY"}
        if secs in exact:
            return f"RRULE:{exact[secs]}"
        for unit, freq in ((86400, "DAILY"), (3600, "HOURLY"), (60, "MINUTELY")):
            if secs % unit == 0:
                return f"RRULE:FREQ={freq};INTERVAL={secs // unit}"
        raise FormatError(f"interval of {secs}s is not expressible as an RRULE")
    raise FormatError(f"{cadence.kind} cadence has no RRULE form")


# --- dispatch -----------------------------------------------------------------

def _detect(text: str) -> str:
    t = text.strip()
    if t.startswith("RRULE:") or t.startswith("FREQ="):
        return "rrule"
    if t.startswith("@") or len(t.split()) in (5, 6):
        return "cron"
    if re.match(r"^(?:R\d*/)?P", t):
        return "iso8601"
    raise FormatError(f"could not detect a schedule dialect for {text!r}")


def parse(text: str, *, dialect: str | None = None) -> Cadence:
    """Parse a schedule string into a Cadence. Auto-detects the dialect when
    not given."""
    dialect = dialect or _detect(text)
    if dialect == "cron":
        return parse_cron(text)
    if dialect == "iso8601":
        return parse_iso8601(text)
    if dialect == "rrule":
        return parse_rrule(text)
    if dialect == "systemd":
        raise NotImplementedError(
            "systemd OnCalendar parsing is the next increment (host-unit "
            "sibling axiom-os#274); cron + iso8601 + rrule ship today."
        )
    raise FormatError(f"unknown dialect: {dialect!r}")


def serialize(cadence: Cadence, *, dialect: str) -> str:
    """Serialize a Cadence to a schedule string in the given dialect."""
    if dialect == "cron":
        return to_cron(cadence)
    if dialect == "iso8601":
        return to_iso8601(cadence)
    if dialect == "rrule":
        return to_rrule(cadence)
    if dialect == "systemd":
        raise NotImplementedError(
            "systemd OnCalendar serialization is the next increment; cron + "
            "iso8601 + rrule ship today."
        )
    raise FormatError(f"unknown dialect: {dialect!r}")


__all__ = [
    "DIALECTS",
    "FormatError",
    "parse",
    "parse_cron",
    "parse_iso8601",
    "parse_rrule",
    "serialize",
    "to_cron",
    "to_iso8601",
    "to_rrule",
]
