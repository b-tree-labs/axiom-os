# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What each button will actually do, said per case.

Founder direction (2026-09-25): "consider a more dynamic and descriptive
variation of each 'Fix', 'Hold', etc where we append: Fix: <method>,
Hold: <consequence and revisit>, Acknowledge: <and which ledger this will
be recorded in>, Discuss: <influential factors to the case>."

A bare verb makes a person guess. "Fix" — with what? "Hold" — and then
what, forever? "Acknowledge" — into what? The answer is different for
every case, so the surface cannot compose it: only the server knows the
declared remedy, the hold window, the ledger, and what this particular
case actually turns on.

The detail is therefore composed HERE, from the same facts the rest of
the page is built from, and it is never aspirational. If a fix is
declared, the method is the fix's own words. If a hold is offered, the
consequence is the window the platform will really keep. If there is no
precedent and no reach, the discuss line says what there IS rather than
inventing something to discuss.

``records`` separates the three decisions from the fourth affordance.
Fix, hold and acknowledge write a row. Discuss opens a conversation and
writes nothing, and a surface that posted it as a decision would be
recording a conversation as a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass

from axiom.extensions.builtins.receipts.cases import Case

#: The ledger a decision lands in, named so the button can say it. There
#: is more than one ledger in this platform, and "recorded" without
#: saying where is the kind of reassurance that means nothing.
DECISION_LEDGER = "the case record"


@dataclass(frozen=True)
class Offer:
    """One thing a person can do about this case, and what it will do."""

    choice: str
    label: str
    detail: str
    #: True = pressing it writes a row. False = it opens something.
    records: bool = True
    #: the capability this will invoke, for the ones that invoke one. A
    #: person about to run something on their own infrastructure is owed
    #: the name of what runs.
    runs: str = ""
    #: what pressing it will NOT do. Acknowledge is the dangerous one:
    #: it stops the asking without changing the situation, and a person
    #: who reads it as "handled" has been misled by the button.
    caveat: str = ""

    def payload(self) -> dict:
        return {
            "choice": self.choice,
            "label": self.label,
            "detail": self.detail,
            "records": self.records,
            "runs": self.runs,
            "caveat": self.caveat,
        }


def _hold_detail(window_hours: int) -> str:
    unit = "a day" if window_hours == 24 else f"{window_hours} hours"
    return f"stay quiet for {unit}, then ask again"


def _discuss_detail(case: Case) -> str:
    """What this case actually turns on, so "Discuss" is not an invitation
    to discuss nothing. Named from what the case carries, never padded."""
    factors: list[str] = []
    cause = case.cause or {}
    roots = [s for s in cause.get("steps", []) if s.get("is_root")]
    if len(roots) > 1:
        factors.append(f"{len(roots)} separate causes")
    elif cause.get("summary") and len(cause.get("steps", [])) > 1:
        factors.append("why these are one problem")
    if case.precedent:
        factors.append("what happened last time")
    blast = case.blast or {}
    if blast.get("declared"):
        factors.append(f"what it touches ({blast.get('summary')})")
    handling = case.handling or {}
    if handling.get("needs_person") and handling.get("because"):
        factors.append("why it reached you")
    if not factors:
        # Nothing composed yet beyond the claims themselves. Say that,
        # rather than promising a richer conversation than we can have.
        return "the evidence on this case"
    return ", ".join(factors)


def offers_for(case: Case, *, hold_window_hours: int = 24) -> list[Offer]:
    """The full offer on this case: the decisions it will accept, plus
    the one affordance that records nothing.

    A decided case is offered nothing to decide — a change of mind is a
    new decision made from the record — but it can still be discussed.
    """
    from axiom.extensions.builtins.receipts.verdicts import ALWAYS_OFFERED

    discuss = Offer(
        choice="discuss",
        label="Discuss",
        detail=_discuss_detail(case),
        records=False,
    )
    if case.verdict:
        return [discuss]

    handling = case.handling or {}
    out: list[Offer] = []
    if handling.get("can_run") and handling.get("fix_summary"):
        # The method is the fix's own sentence, lowercased into the
        # label. Nothing is paraphrased: a button that describes the
        # action differently from the record is two vocabularies.
        summary = str(handling["fix_summary"]).rstrip(".")
        out.append(
            Offer(
                choice="fix",
                label="Fix",
                detail=summary[0].lower() + summary[1:] if summary else "",
                runs=str(handling.get("runs") or ""),
            )
        )
    for choice in ALWAYS_OFFERED:
        if choice == "hold":
            out.append(
                Offer(
                    choice="hold",
                    label="Hold",
                    detail=_hold_detail(hold_window_hours),
                    caveat="Nothing acts on it in the meantime.",
                )
            )
        elif choice == "acknowledge":
            out.append(
                Offer(
                    choice="acknowledge",
                    label="Acknowledge",
                    detail=f"stop asking, and note it in {DECISION_LEDGER}",
                    caveat="This does not change the situation — only the asking.",
                )
            )
    out.append(discuss)
    return out


__all__ = ["DECISION_LEDGER", "Offer", "offers_for"]
