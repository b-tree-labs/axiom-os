# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Uncertainty through a served aggregate.

The boundary this module exists for
-----------------------------------
A conformed row can carry an uncertainty. An aggregate over those rows
could not, so whatever an ingest declared was dropped at the exact point a
reader consumes it — a mean served as a bare number, a peak served without
the interval that says whether it is distinguishable from the next sample.

Why sufficient statistics rather than rows
------------------------------------------
The algebra in this package composes VALUES. A served aggregate must not
pull every row back to compose them: a window can be millions of rows, and
the whole reason the aggregate runs in the database is to avoid that.

So :func:`combine_magnitudes` takes what a single SQL pass can produce —
the count, the sum, the sum of squares, and the largest magnitude — and
reconstructs exactly what :func:`axiom.uncertainty.add` would have returned
from the rows themselves. ``tests/uncertainty/test_serving.py`` proves the
two agree over random inputs, which is what makes the shortcut safe rather
than merely fast.

What a scalar column can and cannot support
-------------------------------------------
``silver.signals.uncertainty`` is a magnitude with no correlation structure.
Per ADR-136 D5 that is ``MagnitudeOnly``, and per D6 combining magnitudes
under unknown correlation yields a **bound**, not a point.

The bound is wide, and it is wide for a real reason that matters here more
than anywhere else in the platform. Consecutive readings from ONE sensor
share that sensor's calibration. They are not independent, so the
root-sum-square answer — the one every naive implementation reports,
because it is what independence gives — understates a long window badly.
Averaging a thousand readings does not average away the calibration. The
honest answer spans from "independent" to "perfectly shared", and says
which end a shared systematic sits at.

Narrowing it is not this module's job to guess. It is the contributing
extension's job to declare structure (spec-uncertainty §2), and when it
does the bound collapses to an exact figure.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

#: What ``low`` rests on when magnitudes arrive without structure.
LOOSE_PREMISE = "unstructured sources are non-negatively correlated"

#: The convention for finding a column's uncertainty sibling. ``value`` is
#: special-cased because the conformed signal shape names its uncertainty
#: column plainly, having only one value column to describe.
VALUE_COLUMN = "value"
VALUE_UNCERTAINTY_COLUMN = "uncertainty"


def uncertainty_column_for(column: str, available: set[str]) -> str | None:
    """Which column carries ``column``'s uncertainty, if any.

    Two forms, both explicit: the conformed signal shape's plain
    ``uncertainty`` beside ``value``, and a ``<column>_uncertainty`` sibling
    for any other column. Nothing is guessed from types — a column that
    happens to be a float is not an error bar.
    """
    if column == VALUE_COLUMN and VALUE_UNCERTAINTY_COLUMN in available:
        return VALUE_UNCERTAINTY_COLUMN
    sibling = f"{column}_uncertainty"
    return sibling if sibling in available else None


#: Columns a companion table uses for the term itself. Everything else it
#: carries is a join key, which is how a companion is discovered without any
#: table name being hardcoded.
TERM_COLUMNS = frozenset({"symbol", "coefficient", "independent"})


def companion_join_keys(companion_columns: set[str]) -> tuple[str, ...] | None:
    """The join keys of a structured-uncertainty companion table.

    The convention, and the whole of it: a companion is named
    ``<table>_uncertainty`` and carries ``symbol``, ``coefficient``,
    ``independent``, plus exactly the columns that identify the row it
    describes. So the join keys are whatever is left over.

    Discovered rather than declared, so a new conformed shape gets a
    companion by following the naming instead of by editing a registry that
    somebody would forget. Returns ``None`` when the table does not carry the
    term columns, because then it is not a companion however it is named.
    """
    if not companion_columns >= TERM_COLUMNS:
        return None
    keys = tuple(sorted(companion_columns - TERM_COLUMNS))
    return keys or None


@dataclass(frozen=True)
class Served:
    """The uncertainty of one served aggregate, and what it could not say.

    ``low`` and ``high`` are ``None`` when nothing is claimable — no rows
    carried an uncertainty, or the aggregate is one for which propagating a
    measurement magnitude would be a fiction. ``None`` is not zero, and the
    distinction is the whole point: zero is a claim of perfect precision.
    """

    fn: str
    low: float | None = None
    high: float | None = None
    low_unconstrained: float = 0.0
    premise: str = "exact"
    #: Rows that carried a value AND an uncertainty.
    quantified: int = 0
    #: Rows that carried a value and NO uncertainty. Outside the bound,
    #: because nothing bounds an unreported quantity (ADR-136 D5).
    unquantified: int = 0
    #: Additional honest detail the aggregate itself forces — an ambiguous
    #: extremum, a dispersion that may be entirely instrumental.
    note: str = ""
    #: The structured sources and their effective coefficients, when any row
    #: declared structure. This is what makes the figure auditable: a reader
    #: can see WHICH source dominates rather than only how wide the answer
    #: is. Empty when nothing declared structure.
    terms: Mapping[str, float] = field(default_factory=dict)

    @property
    def claimable(self) -> bool:
        return self.low is not None and self.high is not None

    @property
    def exact(self) -> bool:
        return self.claimable and self.low == self.high and not self.unquantified

    @property
    def complete(self) -> bool:
        """Whether every row in the window contributed to the bound."""
        return self.unquantified == 0

    @property
    def structured(self) -> bool:
        """Whether any contributing row declared its sources.

        The difference between a figure that can be audited and one that can
        only be bounded.
        """
        return bool(self.terms)

    def dominant(self, *, top: int = 3) -> list[dict]:
        """The largest sources by variance share, which is where effort pays.

        Variance rather than magnitude: uncertainties add in quadrature, so a
        source at half the size of another contributes a quarter as much.
        """
        if not self.terms:
            return []
        total = math.fsum(a * a for a in self.terms.values())
        if total <= 0:
            return []
        ranked = sorted(self.terms.items(), key=lambda kv: -abs(kv[1]))
        return [
            {"symbol": sym, "coefficient": a, "share": (a * a) / total} for sym, a in ranked[:top]
        ]

    def payload(self) -> dict:
        """The wire shape. Absence is transmitted, never omitted."""
        return {
            "low": self.low,
            "high": self.high,
            "low_unconstrained": self.low_unconstrained,
            "premise": self.premise,
            "quantified": self.quantified,
            "unquantified": self.unquantified,
            "complete": self.complete,
            "claimable": self.claimable,
            "note": self.note or None,
            "structured": self.structured,
            "terms": dict(self.terms) if self.terms else None,
            "dominant": self.dominant(),
        }

    def reads(self, *, unit: str = "") -> str:
        """The sentence a person gets."""
        u = f" {unit}" if unit else ""
        if not self.claimable:
            head = self.note or "no row reported an uncertainty, so none is claimed"
            return head
        if self.exact or self.low == self.high:
            body = f"± {self.low:g}{u}"
        else:
            body = (
                f"± between {self.low:g} and {self.high:g}{u} — magnitudes are known, "
                f"how they correlate is not; the lower end assumes {self.premise}"
            )
        if self.unquantified:
            total = self.quantified + self.unquantified
            body += (
                f". That range covers {self.quantified} of {total} rows; "
                f"{self.unquantified} reported no uncertainty and are outside it"
            )
        if self.note:
            body += f". {self.note}"
        return body


def combine_magnitudes(
    *,
    quantified: int,
    sum_u: float,
    sum_sq: float,
    max_u: float,
    unquantified: int = 0,
    scale: float = 1.0,
    fn: str = "sum",
) -> Served:
    """Combine magnitudes from their sufficient statistics.

    Reproduces :func:`axiom.uncertainty.add` over the same magnitudes
    without materialising them, then scales by ``scale`` — ``1/n`` turns a
    sum into a mean, exactly as :func:`axiom.uncertainty.mean` does.

    - ``high = Σuᵢ`` is unconditional: the triangle inequality in L² caps
      the standard deviation of a sum at the sum of standard deviations,
      whatever the correlation.
    - ``low = √(Σuᵢ²)`` holds only for non-negatively correlated sources.
    - ``low_unconstrained = max(0, 2·max(uᵢ) − Σuᵢ)`` assumes nothing and is
      usually zero, which is why it travels alongside rather than as the
      headline.
    """
    if quantified <= 0:
        return Served(
            fn=fn,
            quantified=0,
            unquantified=unquantified,
            note=(
                f"{unquantified} row(s) carried a value and none carried an "
                "uncertainty, so none is claimed"
                if unquantified
                else ""
            ),
        )
    high = sum_u * scale
    low = math.sqrt(max(sum_sq, 0.0)) * scale
    floor = max(0.0, 2 * max_u - sum_u) * scale
    return Served(
        fn=fn,
        low=low,
        high=high,
        low_unconstrained=floor,
        # One magnitude has nothing to correlate with, so the bound is exact
        # and claiming a premise would be noise.
        premise="exact" if quantified == 1 else LOOSE_PREMISE,
        quantified=quantified,
        unquantified=unquantified,
    )


def for_sum(
    *, quantified: int, sum_u: float, sum_sq: float, max_u: float, unquantified: int = 0
) -> Served:
    """A total. Magnitudes add; the bound is the sum's bound."""
    return combine_magnitudes(
        quantified=quantified,
        sum_u=sum_u,
        sum_sq=sum_sq,
        max_u=max_u,
        unquantified=unquantified,
        fn="sum",
    )


def for_mean(
    *, quantified: int, sum_u: float, sum_sq: float, max_u: float, unquantified: int = 0
) -> Served:
    """An average — a sum scaled by 1/n, so correlation carries.

    The divisor is **every row the mean averaged**, quantified or not, which
    is ``quantified + unquantified``. Not the quantified count alone.

    This matters and it was wrong first. The served VALUE is the mean over the
    whole window. Dividing the uncertainty by a smaller count reports the
    uncertainty of a *different quantity* — the mean of whichever subset
    happened to report — and pairing that with the whole window's value is
    incoherent, however cautious the larger number looks.

    A shared source present on ``k`` of ``n`` rows genuinely contributes
    ``a·k/n`` to the mean of all ``n``, because it only perturbs the rows it
    applies to. So more silent rows really do dilute the known contribution,
    and the answer to "then the bound narrows as the data gets worse" is not
    to inflate the divisor. It is :attr:`Served.complete`, which states
    outright that some rows contribute an amount nothing bounds. An honest
    narrow figure with a stated gap beats a wide figure that quietly answers a
    different question.

    It also makes the reconstruction exactly equal to
    :func:`axiom.uncertainty.mean` over the same rows, which divides by
    ``len(items)`` — the unquantified ones included.
    """
    n = quantified + unquantified
    if quantified <= 0 or n <= 0:
        return combine_magnitudes(
            quantified=0,
            sum_u=0.0,
            sum_sq=0.0,
            max_u=0.0,
            unquantified=unquantified,
            fn="mean",
        )
    return combine_magnitudes(
        quantified=quantified,
        sum_u=sum_u,
        sum_sq=sum_sq,
        max_u=max_u,
        unquantified=unquantified,
        scale=1.0 / n,
        fn="mean",
    )


def for_extremum(
    *,
    fn: str,
    selected_u: float | None,
    rivals: int | None,
    quantified: int,
    unquantified: int = 0,
) -> Served:
    """A minimum or maximum — the uncertainty of the row that WON.

    An extremum is not a combination: the served value is one row's
    reading, so its uncertainty is that row's uncertainty and no bound is
    needed.

    What IS uncertain, and what nothing reported before, is **which row
    won**. ``rivals`` counts the other rows lying within the selected row's
    own interval. When it is non-zero the reported peak is not
    distinguishable from those samples, and a reader treating it as the
    location of a real extremum is reading noise. That sentence has to
    travel with the number, because the number alone looks decisive.
    """
    if selected_u is None:
        return Served(
            fn=fn,
            quantified=quantified,
            unquantified=unquantified,
            note=f"the {fn} row reported no uncertainty, so none is claimed for it",
        )
    note = ""
    if rivals is None:
        # Not computed. Said plainly, because a silent zero here would read
        # as "the extremum is unambiguous" — a positive claim nobody made.
        note = (
            f"whether other rows fall within that interval, and so whether this "
            f"{fn} is distinguishable from them, was not determined"
        )
    elif rivals > 0:
        note = (
            f"{rivals} other row(s) fall within that interval, so which row is the "
            f"{fn} is not determined by the data"
        )
    return Served(
        fn=fn,
        low=abs(selected_u),
        high=abs(selected_u),
        low_unconstrained=abs(selected_u),
        premise="exact",
        quantified=quantified,
        unquantified=unquantified,
        note=note,
    )


def for_dispersion(
    *, observed: float | None, mean_u: float | None, quantified: int, unquantified: int = 0
) -> Served:
    """A standard deviation, which measurement error INFLATES.

    Propagating a measurement magnitude into a sample standard deviation as
    though it were another additive term would be wrong: the observed
    dispersion already contains it. For independent errors
    ``s²_observed ≈ s²_true + ū²``, so the measurement magnitude is a FLOOR
    on the spread rather than an addition to it.

    The useful and uncomfortable consequence is reported instead: when the
    mean magnitude is comparable to the observed dispersion, the spread may
    be instrumental rather than real, and anything downstream reading it as
    process variation is reading the instrument.
    """
    if mean_u is None or quantified <= 0:
        return Served(
            fn="std",
            quantified=quantified,
            unquantified=unquantified,
            note=(
                "no row reported an uncertainty, so how much of this spread is "
                "measurement error is unknown"
            ),
        )
    note = (
        f"measurement magnitude averages {mean_u:g}; observed dispersion includes it "
        "rather than adding to it"
    )
    if observed is not None and observed <= mean_u:
        note = (
            f"observed dispersion {observed:g} does not exceed the mean measurement "
            f"magnitude {mean_u:g} — this spread may be entirely instrumental, and "
            "reading it as real variation would be reading the instrument"
        )
    # Deliberately not claimable: a dispersion's own uncertainty is a
    # different quantity from the measurement magnitude, and quoting the
    # latter as the former is the fiction this function refuses.
    return Served(
        fn="std",
        quantified=quantified,
        unquantified=unquantified,
        note=note,
    )


def for_count(*, quantified: int, unquantified: int = 0) -> Served:
    """A count of rows. Exactly known, and not by accident.

    Zero here is a true zero rather than an unreported one, which is the
    one place in this package where zero is the honest answer.
    """
    return Served(
        fn="count",
        low=0.0,
        high=0.0,
        low_unconstrained=0.0,
        premise="exact",
        quantified=quantified,
        unquantified=unquantified,
        note="a count of rows is exactly known",
    )


__all__ = [
    "LOOSE_PREMISE",
    "VALUE_COLUMN",
    "VALUE_UNCERTAINTY_COLUMN",
    "TERM_COLUMNS",
    "Served",
    "companion_join_keys",
    "combine_magnitudes",
    "combine_structured",
    "effective_coefficient",
    "for_count",
    "for_dispersion",
    "for_extremum",
    "for_mean",
    "for_sum",
    "uncertainty_column_for",
]


def effective_coefficient(
    *, sum_a: float, sum_sq: float, independent: bool, scale: float = 1.0
) -> float:
    """One symbol's combined coefficient across the rows of a window.

    The whole reason the companion table carries ``independent``, and the
    field that decides whether a shared error averages away.

    - **Shared** (``independent=False``) — a calibration offset, a reference
      junction, a common supply. Every row's contribution is the SAME draw of
      the same source, so coefficients **add**: ``Σaᵢ``. Scaled by ``1/n`` for
      a mean this returns the coefficient itself, which is the correct and
      unwelcome answer — averaging a thousand readings does not average away
      the calibration.
    - **Per-reading** (``independent=True``) — repeatability, quantisation.
      Each row is a fresh independent draw, so they add in quadrature:
      ``√(Σaᵢ²)``. Scaled by ``1/n`` this shrinks like ``1/√n``, which is the
      averaging-down everybody expects and which only this kind earns.

    Getting the flag backwards is the single most consequential modelling
    error available here, which is why the column defaults to shared.
    """
    combined = sum_a if not independent else math.sqrt(max(sum_sq, 0.0))
    return combined * scale


def combine_structured(
    *,
    terms: Mapping[str, float],
    loose_quantified: int = 0,
    loose_sum_u: float = 0.0,
    loose_sum_sq: float = 0.0,
    loose_max_u: float = 0.0,
    unquantified: int = 0,
    structured_rows: int = 0,
    scale: float = 1.0,
    fn: str = "mean",
) -> Served:
    """Combine declared structure with whatever arrived without it.

    Mirrors :func:`axiom.uncertainty.add`'s three-kind handling at the served
    boundary, and the three kinds are the point (ADR-136 D5):

    - **structured** rows composed EXACTLY, correlation computed from shared
      symbols rather than assumed;
    - **magnitude-only** rows bounded, because nothing says how they correlate;
    - **unreported** rows counted and left OUTSIDE the bound, because nothing
      bounds an unreported quantity.

    A row that declared structure does NOT also contribute its scalar. The
    scalar is a summary of the same sources, and counting both would double
    count — the caller is responsible for the split, and the SQL that does it
    is what makes the split possible in one pass.

    With nothing loose and nothing unreported the result is exact, and the
    bound collapses to a single figure. That collapse is the whole payoff of
    declaring provenance: **declaring more makes the answer narrower**, which
    puts the incentive the right way round.
    """
    structured = math.hypot(*terms.values())
    # `terms` arrive already scaled, because a structured source has to be
    # scaled per symbol — shared and per-reading sources scale differently.
    # The loose magnitudes have no structure to respect, so they are scaled
    # here, by the SAME divisor. Leaving them unscaled was a real bug: a mean
    # over 100 magnitude-only rows reported a bound 100 times too wide, and
    # every mixed window inherited it.
    loose_sum_u = loose_sum_u * scale
    loose_low_sq = max(loose_sum_sq, 0.0) * scale * scale
    if not terms and loose_quantified <= 0:
        return Served(
            fn=fn,
            quantified=0,
            unquantified=unquantified,
            note=(
                f"{unquantified} row(s) carried a value and none carried an "
                "uncertainty, so none is claimed"
                if unquantified
                else ""
            ),
        )

    magnitudes = ([structured] if structured else []) + ([loose_sum_u] if loose_quantified else [])
    low = math.hypot(structured, math.sqrt(loose_low_sq))
    high = math.fsum(magnitudes)
    floor = max(0.0, 2 * max(magnitudes, default=0.0) - high)
    return Served(
        fn=fn,
        low=low,
        high=high,
        low_unconstrained=floor,
        premise="exact" if loose_quantified <= 0 else LOOSE_PREMISE,
        quantified=structured_rows + loose_quantified,
        unquantified=unquantified,
        terms=dict(terms),
        note=(
            f"{loose_quantified} row(s) reported a magnitude without its sources, "
            "so the answer is a range rather than a figure"
            if loose_quantified > 0 and terms
            else ""
        ),
    )
