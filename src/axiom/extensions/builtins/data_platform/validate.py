# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The VALIDATE stage: judge a frame, then say what to record about it.

## The gap this fills

ADR-132's fault taxonomy exists and has never fired. `quality` reads `good` on
all 12,967 fuel-temperature readings of exactly 0 degC, because there was no
stage between acquisition and serving with the authority to say otherwise.
:mod:`company` can now find them; this decides what the store should hold.

## What it records, and why each field

**`bad` nulls the value and keeps `raw_value`.** ADR-132's rule, and the reason
is arithmetic: a fabricated 0 degC left in place is averaged, maxed and charted
as a temperature, while NULL is drawn as a gap and excluded from `avg` by SQL
itself. Keeping `raw_value` is what makes the decision reversible — a later
reader can see exactly what the instrument said and disagree with us.

**`suspect` keeps the value and WIDENS its uncertainty.** The reading is not
known to be wrong, only implicated, so removing it would delete evidence. But it
may no longer be quoted as tightly.

**It never narrows.** ADR-136: anyone may widen, nobody may narrow by assertion.
A validate stage that could tighten an uncertainty would be a place to make a
figure look better by asserting something about it.

**It never reports zero for unknown.** ADR-136 again: zero is a claim of perfect
precision. Where nothing was declared, this says which of the three kinds of
absence applies — sources known, magnitude only, or nothing reported — and the
caller records that, rather than a reassuring 0.0.

## Validation is a CONSTRAINT, not a term

The subtle one, and ADR-136's first decision. When a rule compares a measurement
against its model twin and they disagree, the disagreement must NOT be added to
the uncertainty budget in quadrature: the measurement already contains the
numerical, input and model-form error at those conditions, so adding it double
counts.

What the comparison legitimately contributes is only the part the declared
account does not explain — `sqrt(u_val**2 - sum(u_declared**2))`. That shortfall
is the real diagnostic: **a positive gap means a genuine error source is missing
from the budget**, and refining the largest declared term cannot close it. With
the account complete the gap is zero and validation adds nothing, which is
correct: a value may not be quoted worse than its own account either.

`axiom.uncertainty.pipeline` already implements this as `unexplained()`. This
module computes the same quantity for a frame comparison and hands it over.

## Why this returns a description rather than a `Quantity`

It emits `widen_by` and an `absence` kind, not constructed uncertainty objects.
Two reasons. The stage must be usable where the uncertainty apparatus is not yet
installed — and in that state it has to report absence honestly rather than
import-error or invent a zero. And building the objects at the boundary keeps one
place responsible for symbol naming, which ADR-136 requires be namespaced
`<extension>:<resource>:<aspect>` and qualified when it crosses a node.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .company import QUALITY, Reading, Rule, judge

#: ADR-136's three kinds of absence. Never a fourth, and never zero.
SOURCES_KNOWN = "sources-known"
MAGNITUDE_ONLY = "magnitude-only"
NOTHING_REPORTED = "nothing-reported"
ABSENCE = (SOURCES_KNOWN, MAGNITUDE_ONLY, NOTHING_REPORTED)

#: The symbol this stage contributes under, per ADR-136's naming rule. One
#: symbol for the whole stage: a shortfall found by comparing two channels is
#: not independent of one found the same way on the next row, and minting a
#: symbol per row would let a thousand of them average away.
SHORTFALL_SYMBOL = "data_platform:validate:unexplained"


@dataclass(frozen=True)
class Validated:
    """What the store should hold for one channel at one instant."""

    channel: str
    value: float | None
    raw_value: float | None
    quality: str
    quality_reason: str = ""
    because: str = ""
    #: How much to WIDEN the declared uncertainty by, in the channel's own unit.
    #: Zero means the declared account already explains what validation saw.
    widen_by: float = 0.0
    #: Which of ADR-136's three kinds of absence the caller should record when it
    #: has no declared budget to widen.
    absence: str = SOURCES_KNOWN

    def __post_init__(self) -> None:
        if self.quality not in QUALITY:
            raise ValueError(f"{self.quality!r} is not in ADR-132's closed vocabulary {QUALITY}")
        if self.absence not in ABSENCE:
            raise ValueError(f"{self.absence!r} is not one of {ABSENCE}")
        if self.quality == "bad" and self.value is not None:
            raise ValueError(
                f"{self.channel}: quality 'bad' must null the value — a fabricated "
                "reading left in place is averaged and charted as a real one"
            )
        if self.widen_by < 0:
            raise ValueError(
                f"{self.channel}: widen_by {self.widen_by} is negative, which would "
                "NARROW an uncertainty; ADR-136 forbids narrowing by assertion"
            )


def unexplained(observed: float, declared: list[float]) -> float:
    """`sqrt(observed**2 - sum(declared**2))`, floored at zero.

    ADR-136's validation-as-constraint. Zero when the declared account already
    covers what validation saw — not a negative number and not the observed
    figure, because a value may not be quoted worse than its own account.
    """
    budget = math.fsum(d * d for d in declared)
    gap = observed * observed - budget
    return math.sqrt(gap) if gap > 0 else 0.0


@dataclass(frozen=True)
class Declared:
    """What a channel's uncertainty budget already claims, per channel.

    `terms` are the declared standard uncertainties in the channel's unit.
    Absent from this mapping means nobody declared anything — which is
    `NOTHING_REPORTED`, never zero.
    """

    terms: dict[str, list[float]] = field(default_factory=dict)

    def for_channel(self, channel: str) -> list[float] | None:
        return self.terms.get(channel)


def run(
    frame: dict[str, Reading],
    rules: list[Rule],
    *,
    declared: Declared | None = None,
) -> list[Validated]:
    """Judge `frame` and return what to record for every channel it touches.

    Channels no rule mentions are not returned: silence about a channel is the
    correct output for a channel nothing was found wrong with, and emitting a
    `good` row for all of them would bury the findings in the answer.
    """
    declared = declared or Declared()
    verdicts = {v.channel: v for v in judge(frame, rules)}
    out: list[Validated] = []

    for channel in sorted(verdicts):
        v = verdicts[channel]
        reading = frame.get(channel)
        raw = None if reading is None else reading.value

        if v.quality == "bad":
            # No value survives, so there is nothing left to qualify. The
            # absence recorded is about the uncertainty of a value that is now
            # gone — `NOTHING_REPORTED` rather than a bound on nothing.
            out.append(
                Validated(channel, None, raw, "bad", v.reason, v.because, absence=NOTHING_REPORTED)
            )
            continue

        widen = 0.0
        absence = SOURCES_KNOWN
        budget = declared.for_channel(channel)
        if budget is None:
            # Nothing declared. This is the common real state and it must be
            # visible: an aggregate over it cannot be claimed, and saying so is
            # the difference between an honest gap and a confident zero.
            absence = NOTHING_REPORTED
        elif v.observed is not None:
            widen = unexplained(v.observed, budget)
            if widen > 0:
                # A positive gap is the finding: something not in the budget is
                # contributing this much, and refining the largest declared term
                # cannot close it.
                absence = MAGNITUDE_ONLY
        out.append(
            Validated(
                channel, raw, raw, v.quality, v.reason, v.because, widen_by=widen, absence=absence
            )
        )
    return out


__all__ = [
    "ABSENCE",
    "MAGNITUDE_ONLY",
    "NOTHING_REPORTED",
    "SHORTFALL_SYMBOL",
    "SOURCES_KNOWN",
    "Declared",
    "Validated",
    "run",
    "unexplained",
]
