# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Two compound reads: comparing peers, and comparing a model to a measurement.

Both are joins, and both were being done client-side. That is where they went
wrong — not because a join is hard, but because the things that must travel
*with* the numbers are the first casualties of assembling a picture from two
separate fetches.

``series_compare`` answers "show me this measurement across these loops". It is
only possible at all because a channel now declares a **role**: VCU's STC1 and
TAMU's Tc_01 are both ``fluid_temperature``, so the comparison groups on what a
channel means rather than on what it is called, and neither site has to rename
anything.

``parity`` answers "how far is the model from the measurement". Fetching the two
series separately and subtracting them loses two things. The provenance, because
once both are arrays of floats nothing says which was predicted. And the pairing
rule, because points that exist on one side and not the other have to be dropped
to subtract, and a silent drop turns a coverage gap into an apparent agreement.

So both return what was *excluded* as prominently as what was included. A
comparison that quietly omits a site, or a residual computed over the half of
the window where both happened to have data, is worse than no comparison: it
looks like an answer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

#: Row classes that are not measurements. A comparison including these without
#: being asked is the failure the whole vocabulary exists to prevent.
NON_MEASURED = frozenset({"predicted", "estimated", "simulated"})


@dataclass(frozen=True)
class SeriesForSite:
    site: str
    channel: str
    unit: str | None
    points: tuple[tuple[datetime, float], ...]

    @property
    def n(self) -> int:
        return len(self.points)


@dataclass(frozen=True)
class CompareResult:
    """Peer series, plus everything deliberately left out."""

    role: str
    series: tuple[SeriesForSite, ...]
    #: ``site -> why``. Reported, never silently dropped: a site missing from a
    #: comparison reads as "no data" when it may mean "excluded because its rows
    #: are simulated", and those call for opposite actions.
    excluded: dict[str, str] = field(default_factory=dict)
    #: Units seen across the included series. More than one means the numbers
    #: are not comparable and the caller must be told rather than shown.
    units: tuple[str, ...] = ()

    @property
    def comparable(self) -> bool:
        return len(self.series) >= 2 and len(self.units) <= 1

    @property
    def warnings(self) -> list[str]:
        out: list[str] = []
        if len(self.units) > 1:
            out.append(
                f"{self.role}: series arrive in different units ({', '.join(self.units)}) — "
                "plotting them on one axis would compare different quantities"
            )
        if len(self.series) < 2:
            out.append(
                f"{self.role}: only {len(self.series)} site has data; a comparison "
                "needs at least two"
            )
        return out


def series_compare(
    role: str,
    rows_by_site: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    include_simulated: bool = False,
    include_derived: bool = False,
) -> CompareResult:
    """Gather one role across several sites, saying what was left out and why.

    ``rows_by_site`` maps a site to its rows for this role; each row carries
    ``ts``, ``value``, and the provenance columns silver holds.

    Derived channels are excluded by default. A loop reporting ``T_lm``
    alongside the thermocouples it was computed from would otherwise contribute
    the same physics twice to an average across sites.
    """
    series: list[SeriesForSite] = []
    excluded: dict[str, str] = {}
    units: set[str] = set()

    for site in sorted(rows_by_site):
        rows = list(rows_by_site[site])
        if not rows:
            excluded[site] = "no rows in this window"
            continue

        classes = {str(r.get("source_class") or "measured") for r in rows}
        modelled = classes & NON_MEASURED
        if modelled and not include_simulated:
            excluded[site] = (
                f"rows are {', '.join(sorted(modelled))}, not measured — "
                "pass include_simulated to compare them anyway"
            )
            continue

        kept = [
            r for r in rows
            if include_derived or str(r.get("derivation") or "") != "derived"
        ]
        if not kept:
            excluded[site] = "every channel here is derived, not an independent sensor"
            continue

        by_channel: dict[str, list[Mapping[str, Any]]] = {}
        for row in kept:
            by_channel.setdefault(str(row.get("channel", "")), []).append(row)

        for channel in sorted(by_channel):
            chan_rows = sorted(by_channel[channel], key=lambda r: r["ts"])
            unit = next((r.get("unit") for r in chan_rows if r.get("unit")), None)
            if unit:
                units.add(str(unit))
            series.append(SeriesForSite(
                site=site,
                channel=channel,
                unit=unit,
                points=tuple(
                    (r["ts"], float(r["value"])) for r in chan_rows if r.get("value") is not None
                ),
            ))

    return CompareResult(
        role=role,
        series=tuple(series),
        excluded=excluded,
        units=tuple(sorted(units)),
    )


@dataclass(frozen=True)
class ParityPoint:
    ts: datetime
    measured: float
    predicted: float

    @property
    def residual(self) -> float:
        """Predicted minus measured. Sign is a convention, so it is stated:
        positive means the model is running high."""
        return self.predicted - self.measured


@dataclass(frozen=True)
class ParityResult:
    """A model against a measurement, and the points that could not be paired."""

    paired: tuple[ParityPoint, ...]
    unpaired_measured: int = 0
    unpaired_predicted: int = 0
    unit: str | None = None
    model_ref: str | None = None
    tolerance: timedelta = timedelta(0)

    @property
    def provenance(self) -> str:
        """Never 'measured'. The pair is a hybrid by construction."""
        return "hybrid"

    @property
    def coverage(self) -> float:
        total = len(self.paired) + self.unpaired_measured + self.unpaired_predicted
        return len(self.paired) / total if total else 0.0

    @property
    def warnings(self) -> list[str]:
        out: list[str] = []
        if self.unpaired_measured or self.unpaired_predicted:
            out.append(
                f"{self.unpaired_measured} measured and {self.unpaired_predicted} predicted "
                f"points had no counterpart within {self.tolerance} and are excluded from "
                "the residual — a residual computed only where both happened to have data "
                "reads as agreement"
            )
        if not self.model_ref:
            out.append(
                "no model_ref on the predicted rows — this residual cannot be attributed "
                "to a model version, so it cannot be reproduced or retired"
            )
        return out

    def residuals(self) -> tuple[tuple[datetime, float], ...]:
        return tuple((p.ts, p.residual) for p in self.paired)


def parity(
    measured: Sequence[Mapping[str, Any]],
    predicted: Sequence[Mapping[str, Any]],
    *,
    tolerance: timedelta = timedelta(seconds=1),
) -> ParityResult:
    """Pair a measured series with a predicted one and report the residual.

    Pairing is nearest-in-time within ``tolerance``, each point used once.
    Points with no counterpart are **counted, not dropped**: the residual over
    the subset where both sides happen to have data is the most flattering
    possible view of a model, and it is the one you get for free by subtracting
    two arrays.
    """
    m_sorted = sorted((r for r in measured if r.get("value") is not None), key=lambda r: r["ts"])
    p_sorted = sorted((r for r in predicted if r.get("value") is not None), key=lambda r: r["ts"])

    unit = next((r.get("unit") for r in m_sorted if r.get("unit")), None)
    model_ref = next((r.get("model_ref") for r in p_sorted if r.get("model_ref")), None)

    paired: list[ParityPoint] = []
    used_p: set[int] = set()
    j = 0
    for m in m_sorted:
        best: int | None = None
        best_gap: timedelta | None = None
        k = j
        while k < len(p_sorted):
            gap = p_sorted[k]["ts"] - m["ts"]
            if gap < -tolerance:
                k += 1
                j = k
                continue
            if gap > tolerance:
                break
            if k not in used_p:
                agap = abs(gap)
                if best_gap is None or agap < best_gap:
                    best, best_gap = k, agap
            k += 1
        if best is not None:
            used_p.add(best)
            paired.append(ParityPoint(
                ts=m["ts"],
                measured=float(m["value"]),
                predicted=float(p_sorted[best]["value"]),
            ))

    return ParityResult(
        paired=tuple(paired),
        unpaired_measured=len(m_sorted) - len(paired),
        unpaired_predicted=len(p_sorted) - len(used_p),
        unit=str(unit) if unit else None,
        model_ref=str(model_ref) if model_ref else None,
        tolerance=tolerance,
    )


__all__ = [
    "NON_MEASURED",
    "CompareResult",
    "ParityPoint",
    "ParityResult",
    "SeriesForSite",
    "parity",
    "series_compare",
]
