# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One reading that says how well it is known — the pattern to copy.

``one_reading`` is the simplest normalizer that is still correct. This is the
simplest one that is also *composable*, and the difference is the whole of
ADR-136.

Why a scalar is not enough
--------------------------
A single ``uncertainty`` number per row is honest and cannot be combined. Two
readings from one instrument share that instrument's calibration, so averaging
them as though they were independent claims a precision the instrument cannot
deliver — always in the overconfident direction, and invisibly, because the
two scalars look exactly like two independent ones.

Declaring the SOURCES fixes it without anyone having to remember to. Averaging
a thousand readings that share a calibration bath returns the bath's full
magnitude, because the shared symbol says they share it.

The two kinds, and the one that bites
-------------------------------------
``uncertainty_terms`` maps a symbol to its coefficient, or to
``(coefficient, independent)``.

``independent=False`` (the default) means the source is **shared** across rows
— a calibration offset, a reference junction, a common supply. Coefficients
sum, so a mean keeps the whole offset.

``independent=True`` means a **fresh draw per reading** — repeatability,
quantisation. Those add in quadrature and shrink as ``1/√n``.

Getting that flag backwards is the most consequential mistake available here.
Declaring a shared bath as per-reading claims it averages away: at 400 readings
that is a figure twenty times too confident, and it looks entirely reasonable
on a chart. Which is why the default is shared — a wrong ``False`` is merely a
wide bound, a wrong ``True`` is a confident wrong answer.

Where the numbers come from
---------------------------
**Not from here.** The magnitudes below are read off the record, because which
instrument shares which calibration standard is the site's own knowledge and
belongs in the site's channel map as data. A platform example that invented
plausible-looking coefficients would be worse than one that invents nothing:
somebody would copy them.

What this file fixes in place is the SHAPE — the symbol grammar, the two kinds,
and a budget that names its measurand.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from axiom.uncertainty import TYPE_A, TYPE_B, Budget, check_symbol

#: The schema_ref this normalizer claims. Registration keys on it exactly.
SCHEMA_REF = "example.reading-with-uncertainty/v1"

#: The extension namespace this normalizer's symbols live under. A symbol is
#: ``<extension>:<resource>:<aspect>`` so that nobody has to guess whose
#: source it is, and so two extensions cannot collide (ADR-136 D3).
NAMESPACE = "example"


def calibration_symbol(standard: str) -> str:
    """The symbol for a calibration standard.

    ONE standard is ONE symbol however many channels it calibrates. That is
    the point: sharing a symbol is what makes the shared error refuse to
    average away, and minting a per-channel symbol for a shared standard is
    the mistake that makes it vanish.
    """
    return check_symbol(f"{NAMESPACE}:{standard}:offset")


def repeatability_symbol(channel: str) -> str:
    """The symbol for one channel's per-reading noise.

    Per CHANNEL, not per reading. The companion table's ``independent`` flag
    is what says each row is a fresh draw — a symbol per row would explode the
    symbol space to no purpose and make the budget unreadable.
    """
    return check_symbol(f"{NAMESPACE}:{channel}:repeatability")


def budgets(record: dict[str, Any]) -> list[Budget]:
    """The budgets for the symbols this record's row declares.

    Separate from the rows because a budget describes an INSTRUMENT and a row
    describes a moment. The sensor's calibration uncertainty is a
    property of the sensor, and every reading it ever produces refers to
    the same budget — so writing it per row would be a thousand copies of one
    fact, and the thousandth could disagree with the first.
    """
    payload = record["row"]
    channel = str(payload["channel"])
    unit = payload.get("unit", "")
    out: list[Budget] = []

    cal = payload.get("calibration") or {}
    if cal.get("standard") and cal.get("uncertainty") is not None:
        out.append(
            Budget(
                symbol=calibration_symbol(str(cal["standard"])),
                # Without a measurand an uncertainty is not merely unexplained,
                # it is undefined. "the sensor" is not a measurand.
                measurand=(
                    f"value indicated by a channel calibrated against "
                    f"{cal['standard']}, in {unit or 'the channel unit'}"
                ),
                standard=float(cal["uncertainty"]),
                kind=TYPE_B,
                traceable_to=str(cal.get("traceable_to") or "unstated"),
                valid_over=str(cal.get("valid_over") or "unstated"),
            )
        )

    rep = payload.get("repeatability") or {}
    if rep.get("uncertainty") is not None:
        n = rep.get("observations")
        out.append(
            Budget(
                symbol=repeatability_symbol(channel),
                measurand=f"repeatability of channel {channel}",
                standard=float(rep["uncertainty"]),
                # Evaluated from repeated observation when the source says how
                # many. That count is not bookkeeping: it sets the coverage
                # factor, and at six observations the honest 95% factor is
                # 2.57 rather than the habitual 2.
                kind=TYPE_A if n else TYPE_B,
                dof=float(n - 1) if n and int(n) > 1 else Budget.dof,
                traceable_to=(f"{n} repeated observations" if n else "unstated"),
                valid_over=str(rep.get("valid_over") or "unstated"),
            )
        )
    return out


def reading_with_uncertainty(record: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """One bronze row into one canonical signal that knows its own sources.

    ``site``, ``schema_ref`` and ``row_hash`` are filled in by
    ``conform_rows``; setting them here would duplicate or disagree.
    """
    payload = record["row"]
    channel = str(payload["channel"])

    # Raise rather than emit a row you do not believe.
    value = float(payload["value"])

    terms: dict[str, tuple[float, bool]] = {}

    cal = payload.get("calibration") or {}
    if cal.get("standard") and cal.get("uncertainty") is not None:
        # independent=False: every channel on this standard shares this draw.
        terms[calibration_symbol(str(cal["standard"]))] = (float(cal["uncertainty"]), False)

    rep = payload.get("repeatability") or {}
    if rep.get("uncertainty") is not None:
        # independent=True: a fresh draw each reading, so it averages down.
        terms[repeatability_symbol(channel)] = (float(rep["uncertainty"]), True)

    row: dict[str, Any] = {
        "stream": str(payload.get("stream") or "example"),
        "channel": channel,
        "ts": payload["ts"],
        "value": value,
        "unit": payload.get("unit", ""),
        "source_class": "measured",
        "derivation": "raw",
    }

    if terms:
        row["uncertainty_terms"] = terms
        # The scalar stays, as a SUMMARY for readers that cannot compose. It
        # is derived from the same terms rather than supplied separately, so
        # the two cannot disagree — and the served path knows to use the terms
        # and ignore this, so it is never double counted.
        row["uncertainty"] = sum(c * c for c, _ in terms.values()) ** 0.5
    # No `else` writing 0.0. Zero is a claim of perfect precision; a source
    # that said nothing made no claim, and NULL is how that is recorded.

    yield row


__all__ = [
    "NAMESPACE",
    "SCHEMA_REF",
    "budgets",
    "calibration_symbol",
    "reading_with_uncertainty",
    "repeatability_symbol",
]
