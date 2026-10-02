# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""How many digits to show is a scientific question, not a formatting one.

The table renderer formatted every float with ``:g`` — six significant
figures. That is wrong in both directions at once, and both are the kind of
wrong that looks fine.

**It invents precision.** Padding ``51.2`` to ``51.200`` asserts the
instrument resolved thousandths. A reader cannot tell a measured digit from
an added zero, so the display makes a claim the data does not support.

**It destroys precision.** ``f"{1234.5678:g}"`` is ``'1234.57'``. Two digits
the source carried are gone. Somebody who copies that number into a
calculation has been handed a value wrong by more than the instrument's own
error, and every step after compounds it. Display rounding is a
contribution to aggregate uncertainty that nobody budgeted for and nobody
can see.

The rules, in order:

1. **Never show a digit the value does not have.** Python's ``repr`` of a
   float is the shortest string that round-trips — exactly the digits the
   value carries, no more. That is the default, and it is not a
   coincidence that it is also the honest one.
2. **Never drop a digit the value does have**, unless something says the
   digit is not meaningful.
3. **Uncertainty is what says so.** ``51.2456 ± 0.01`` is significant to
   the hundredths: more is noise, less is loss. Where a row carries an
   uncertainty, it governs, by the ordinary convention of rounding the
   value to the uncertainty's own decimal place.
4. **A column is read down.** Aligning on the decimal point lets every
   value keep its own precision while the digits still line up — the way a
   scientific table has always been set, and the reason it does not need
   to pad each cell to a common number of decimals.

What this module will not do: choose a "nice" number of digits. There is no
such thing independent of the measurement, and inventing one is how a
display becomes an unreviewed claim about instrument resolution.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

#: Shown where there is no value. Not an empty cell — empty reads as
#: "nothing here", a dash reads as "we looked and there is none".
ABSENT = "—"

#: Beyond these, a plain decimal expansion is more digits than a reader can
#: hold and tells them less than the exponent does. The thresholds are about
#: legibility only; inside them nothing is abbreviated.
_SCI_LARGE = 1e16
_SCI_SMALL = 1e-5


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _shortest(value: float) -> str:
    """The shortest decimal string that reads back as this exact float.

    ``repr`` already guarantees this. Using it rather than a fixed
    precision is the whole of rule 1 and rule 2: it cannot add a digit the
    value does not have, and it cannot drop one it does.
    """
    text = repr(float(value))
    if text.endswith(".0"):
        return text[:-2]
    return text


def _decimals_for(uncertainty: float) -> int | None:
    """Which decimal place an uncertainty makes meaningful.

    The ordinary convention: the value is rounded to the same place as the
    uncertainty's leading significant digit. ``±0.01`` gives two decimals.
    """
    if not math.isfinite(uncertainty) or uncertainty <= 0:
        # Zero means unstated, not exact — an instrument with no error is
        # not a thing, and treating it as one would print every digit a
        # float has as though each were measured.
        return None
    return max(0, -math.floor(math.log10(abs(uncertainty))))


def format_number(value: Any, *, uncertainty: float | None = None) -> str:
    """One value, at the precision it actually has.

    ``uncertainty`` overrides, because it is the only thing that can say a
    digit is not meaningful.
    """
    if value is None:
        return ABSENT
    if isinstance(value, bool):
        # An int in Python and a state everywhere else. Rendering it as 1
        # would throw away which was meant.
        return str(value)
    if not _is_number(value):
        return str(value)
    if isinstance(value, int):
        return str(value)

    number = float(value)
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "Infinity" if number > 0 else "-Infinity"

    if uncertainty is not None:
        decimals = _decimals_for(float(uncertainty))
        if decimals is not None:
            # Decimal, not round(), so the rounding is the decimal one a
            # reader expects rather than binary-float's nearest-even.
            quantized = Decimal(repr(number)).quantize(Decimal(1).scaleb(-decimals))
            text = format(quantized, "f")
            return text.rstrip("0").rstrip(".") if "." in text else text

    magnitude = abs(number)
    if magnitude != 0 and (magnitude >= _SCI_LARGE or magnitude < _SCI_SMALL):
        # repr already switches to exponent form at these magnitudes and
        # still round-trips, so this is a legibility branch, not a
        # precision one.
        return _shortest(number)
    return _shortest(number)


def align_column(
    values: list[Any], *, uncertainties: list[float | None] | None = None
) -> list[str]:
    """A column of numbers, aligned on the decimal point.

    Every value keeps its own precision. Nothing is padded with invented
    digits; the padding is spaces, which claim nothing.
    """
    if not values:
        return []
    uncertainties = uncertainties or [None] * len(values)
    rendered = [
        format_number(v, uncertainty=u)
        for v, u in zip(values, uncertainties, strict=False)
    ]

    heads: list[str] = []
    tails: list[str] = []
    for text in rendered:
        if "." in text and "e" not in text.lower():
            head, tail = text.split(".", 1)
            heads.append(head)
            tails.append("." + tail)
        else:
            heads.append(text)
            tails.append("")

    head_width = max(len(h) for h in heads)
    tail_width = max(len(t) for t in tails)
    return [h.rjust(head_width) + t.ljust(tail_width) for h, t in zip(heads, tails, strict=False)]


__all__ = ["ABSENT", "align_column", "format_number"]
