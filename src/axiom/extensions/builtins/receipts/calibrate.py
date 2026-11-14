# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""How often a solver's judgement was followed by the outcome it implied.

This is the differentiator, and until now it was specified and absent —
so everything shipped was a well-written incident console. Spec §3 names
the operation; this is it.

The question it answers, which nothing else can
-----------------------------------------------
Not "is the fleet up". Not "did the model drift". **When this solver says
hold, how often does the situation clear on its own? When it says fix, how
often does it actually resolve?** Per solver, per condition.

A ticketing system records what somebody did and never watches whether it
worked. A monitoring stack watches the world and never records who decided
what about it. The join of the two — a decision carrying the options that
existed, attributed, with an outcome observed independently of whoever
decided — is the substrate this measurement needs, and it is why the
record had to come first.

Why it returns a BOUND and not a number
---------------------------------------
The record can say a decision's situation went away (``outcome =
resolved``). It cannot say a situation *failed* to go away, because
``outcome IS NULL`` means two different things: still open, or open for
ever. That is censoring, and spec §3 already says so.

So this reports an interval. The lower bound assumes every unresolved
decision never clears; the upper bound assumes they all eventually do. The
truth is inside. A point estimate here would be the single most dangerous
number this platform could print — it would look like a measurement of
judgement and would be an artifact of how recently somebody decided.

The bound narrows as decisions resolve. A caller who wants certainty
should wait, not round.

What this deliberately does NOT do
----------------------------------
- **No threshold.** Nothing here says a solver is good enough. That is a
  grant of authority, it belongs to a person, and ADR-135 says which gates
  it passes through.
- **No averaging across conditions.** A solver excellent at silent
  reporters and poor at contradicted evidence has no meaningful single
  number, and one would license them at the thing they are worst at.
  Callers asking for an overall figure get per-condition rows and may
  combine them knowing what they are doing.
- **No inference about the future.** It is a reading of what happened.
"""

from __future__ import annotations

from dataclasses import dataclass

from axiom.extensions.builtins.receipts.db_models import CaseVerdict
from axiom.extensions.builtins.receipts.verdicts import OUTCOME_RESOLVED


@dataclass(frozen=True)
class Calibration:
    """One solver, one condition, one choice — and what followed."""

    solver: str
    solver_kind: str
    condition: str
    chosen: str
    #: decisions whose situation was observed to go away
    resolved: int
    #: decisions still without an observed outcome. NOT failures — the
    #: distinction the record cannot make, carried rather than collapsed.
    open: int

    @property
    def decided(self) -> int:
        return self.resolved + self.open

    @property
    def low(self) -> float:
        """If none of the open ones ever clears."""
        return self.resolved / self.decided if self.decided else 0.0

    @property
    def high(self) -> float:
        """If all of them do."""
        if not self.decided:
            return 0.0
        return (self.resolved + self.open) / self.decided

    def reads(self) -> str:
        """The sentence a person sees. States the bound as a bound."""
        if not self.decided:
            return f"{self.solver} has decided nothing of this kind."
        if not self.open:
            return (
                f"{self.solver} chose {self.chosen} {self.decided} times here, and "
                f"{self.resolved} cleared — {round(self.low * 100)}%."
            )
        return (
            f"{self.solver} chose {self.chosen} {self.decided} times here. "
            f"{self.resolved} cleared and {self.open} have not been observed either "
            f"way, so the rate is between {round(self.low * 100)}% and "
            f"{round(self.high * 100)}%."
        )

    def payload(self) -> dict:
        return {
            "solver": self.solver,
            "solver_kind": self.solver_kind,
            "condition": self.condition,
            "chosen": self.chosen,
            "resolved": self.resolved,
            "open": self.open,
            "decided": self.decided,
            "low": round(self.low, 4),
            "high": round(self.high, 4),
            "reads": self.reads(),
        }


@dataclass(frozen=True)
class CalibrationReport:
    rows: tuple[Calibration, ...]
    #: decisions that could not be measured, and why. Stated rather than
    #: dropped: a measurement that silently excludes half its input is a
    #: worse lie than no measurement.
    excluded_no_condition: int = 0

    def payload(self) -> dict:
        return {
            "rows": [r.payload() for r in self.rows],
            "excluded_no_condition": self.excluded_no_condition,
        }

    def for_solver(self, solver: str) -> list[Calibration]:
        return [r for r in self.rows if r.solver == solver]


def calibrate(
    session,
    *,
    site: str = "",
    solver: str = "",
    condition: str = "",
) -> CalibrationReport:
    """Read the decision record. Never writes, never infers.

    Grouped by ``(solver, condition, chosen)``. The choice is part of the
    key because "how often does this solver's HOLD clear" and "how often
    does its FIX clear" are different questions with different meanings —
    a hold that clears means the situation resolved itself, which is a
    solver correctly declining to act.
    """
    q = session.query(CaseVerdict)
    if site:
        q = q.filter(CaseVerdict.site == site)
    if solver:
        q = q.filter(CaseVerdict.decider == solver)
    if condition:
        q = q.filter(CaseVerdict.condition == condition)

    buckets: dict[tuple[str, str, str, str], list[int]] = {}
    excluded = 0
    for row in q.all():
        if not row.condition:
            # Older rows predate the condition column. Measuring them
            # against a guessed key would be calibration on a foundation
            # of invention; counted instead.
            excluded += 1
            continue
        key = (row.decider, row.decider_kind or "human", row.condition, row.chosen)
        seen = buckets.setdefault(key, [0, 0])
        if row.outcome == OUTCOME_RESOLVED:
            seen[0] += 1
        else:
            seen[1] += 1

    rows = [
        Calibration(
            solver=s,
            solver_kind=kind,
            condition=cond,
            chosen=chose,
            resolved=counts[0],
            open=counts[1],
        )
        for (s, kind, cond, chose), counts in sorted(buckets.items())
    ]
    return CalibrationReport(rows=tuple(rows), excluded_no_condition=excluded)


__all__ = ["Calibration", "CalibrationReport", "calibrate"]
