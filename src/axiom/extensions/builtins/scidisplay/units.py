# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A value whose unit is not declared may not be shown as a bare number.

``chart_svg`` already states the principle in its own docstring — *a number
without its unit is not a measurement* — and then every surface quietly broke
it the same way::

    f" {unit}" if unit else ""
    str(row.get("unit") or "")

An absent unit became the empty string, the number rendered alone, and a
reader saw something indistinguishable from a dimensionless quantity. That is
not a cosmetic gap. Measured on the node on 2026-09-24, **23.2 million of 70.1
million served rows carried no unit, and for one site every single one of its
19.1 million rows did.** Among them ``Power`` ranging to 1,170,000 and
``excess_rho`` ranging 0.53 to 7.81 — a number that reads as routine in one
unit and as impossible in another.

The platform did not say "unknown". It served a number, and a number reads as
a fact.

So: absent is rendered, not skipped. :data:`UNDECLARED` goes where the unit
would have gone, in every surface — chart axis, table header, export column,
terminal, chat. The reader is told the value is incomplete instead of being
handed something that looks finished.

**Why a word and not a blank.** In a CSV an empty cell reads as "no unit
needed"; a literal ``unit not declared`` reads as "this is missing". The
distinction is the entire point, and it only exists if something is written.

**Why not refuse to serve.** Because the data is real and the operator asking
for it knows their own instrument. Withholding it would be its own kind of
wrong answer. What must not happen is presenting it as complete.
"""

from __future__ import annotations

#: What is shown where a unit would be. Never a real unit, and deliberately
#: words rather than a symbol: ``?`` invites the reading "unknown value", and
#: the value is known — it is the unit that is missing.
UNDECLARED = "unit not declared"

#: The shorter form, for a place that is genuinely too tight for the sentence
#: — a legend swatch, a narrow axis. Still never blank.
UNDECLARED_BRIEF = "no unit"


def declared(unit: object) -> bool:
    """Whether *unit* is a unit somebody actually stated.

    ``None``, ``""`` and whitespace are all the same absence. They are told
    apart nowhere else, so they are not told apart here.
    """
    return bool(str(unit).strip()) if unit is not None else False


def label(unit: object, brief: bool = False) -> str:
    """The unit as it should appear — never the empty string."""
    if declared(unit):
        return str(unit).strip()
    return UNDECLARED_BRIEF if brief else UNDECLARED


def qualify(name: str, unit: object, brief: bool = False) -> str:
    """``"Power (W)"``, or ``"Power (unit not declared)"``.

    Used for a column header, an axis title, a legend entry: anywhere a
    quantity is named once and its values follow.
    """
    return f"{name} ({label(unit, brief=brief)})"


def annotate(text: str, unit: object, brief: bool = False) -> str:
    """``"1170000 W"``, or ``"1170000 (unit not declared)"``.

    For a lone value with no header to carry the unit for it. The declared
    case is a bare space, as a measurement is normally written; the absent
    case is parenthesised, because it is a statement about the value rather
    than part of it.
    """
    if declared(unit):
        return f"{text} {str(unit).strip()}"
    return f"{text} ({label(unit, brief=brief)})"


def for_export(unit: object) -> str:
    """The cell to write into a ``unit`` column of a CSV or a spreadsheet.

    Always :data:`UNDECLARED` rather than blank. An empty cell in a
    downstream tool is indistinguishable from a quantity that needs no unit,
    and the whole reason to write this is to make the difference visible to
    somebody who is no longer looking at our screen.
    """
    return label(unit)


def all_declared(units: object) -> bool:
    """Whether every unit in an iterable was stated.

    Lets a surface say once, at the top, that something below it is
    incomplete — rather than making the reader find which row.
    """
    return all(declared(u) for u in (units or ()))


def undeclared_count(units: object) -> int:
    return sum(1 for u in (units or ()) if not declared(u))


__all__ = [
    "UNDECLARED",
    "UNDECLARED_BRIEF",
    "all_declared",
    "annotate",
    "declared",
    "for_export",
    "label",
    "qualify",
    "undeclared_count",
]
