# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Why a person is looking at this case, or why they are not.

Founder framing (2026-09-24): "why can't we just automatically deal with
this already? ... the bar for human intervention needs to be
significant." A surface that forwards every non-green claim to a person
is not oversight, it is a queue with extra steps, and it fails in the
way that is hardest to recover from — people stop reading it.

So the default flips. A case is handled unless one of a SHORT, FINITE
list of facts stands in the way, and the case says which one. Every
entry below is something the platform can check about itself; none of
them is a judgement someone tuned, because a tuned threshold is exactly
what nobody can defend later.

The last of them is the interesting one. ``NEVER_WORKED`` is answered
from the decision record itself: case ids are stable for the same site
and entity, so "has this fix ever actually cleared this case" is a
lookup. A fix EARNS its autonomy by its own track record rather than by
someone deciding it is safe. That is the gray area measured instead of
adjudicated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from axiom.extensions.builtins.receipts.cases import Case
from axiom.extensions.builtins.receipts.conditions import condition_for

#: Stable codes, so "how often does each reason fire" is countable and
#: the next thing to build is chosen from evidence rather than taste.
HELD = "held"
NO_FIX = "no_fix"
NOT_OURS = "not_ours"
TRIED = "tried"
DID_NOT_WORK = "did_not_work"
NEVER_WORKED = "never_worked"
#: Declared on the condition, never computed, and checked before anything
#: else — see Condition.authority_reserved. The one reason no track record
#: can retire.
RESERVED = "reserved"


@dataclass(frozen=True)
class Handling:
    """How this case is being dealt with."""

    #: what the platform would run, in plain words. "" = nothing declared.
    fix_summary: str
    #: a declared fix that applies to THIS case, here, now.
    can_run: bool
    needs_person: bool
    #: the one sentence that answers "why am I being shown this".
    because: str
    #: the capability behind the fix, named. A person about to run
    #: something on their own infrastructure is owed the name of what runs.
    runs: str = ""
    #: "" when nobody needs to be involved.
    reason: str = ""

    def payload(self) -> dict:
        return {
            "fix_summary": self.fix_summary,
            "runs": self.runs,
            "can_run": self.can_run,
            "needs_person": self.needs_person,
            "because": self.because,
            "reason": self.reason,
        }


def root_condition_key(case: Case) -> str:
    """``"<claim_kind>|<status>"`` for the case's ROOT claim.

    The key `calibrate` groups by (spec §3). Derived from the same cause
    chain `root_condition` uses, so the condition a decision is recorded
    against is the one the reader was shown — not a different claim that
    happened to sort first.
    """
    steps = (case.cause or {}).get("steps") or []
    roots = [s for s in steps if s.get("is_root")]
    head = roots[0] if roots else (steps[0] if steps else None)
    if head is not None:
        return f"{head['claim_kind']}|{head['status']}"
    first = case.items[0]
    return f"{first.claim_kind}|{first.status}"


def root_condition(case: Case):
    """The condition of the thing to fix. The cause chain already put the
    root first, so a case with one cause has one fix rather than one per
    symptom."""
    steps = (case.cause or {}).get("steps") or []
    roots = [s for s in steps if s.get("is_root")]
    head = roots[0] if roots else (steps[0] if steps else None)
    if head is not None:
        return condition_for(head["claim_kind"], head["status"])
    first = case.items[0]
    return condition_for(first.claim_kind, first.status)


def handling_for(
    session,
    case: Case,
    *,
    this_node: str = "",
    require_precedent: bool = False,
    now: datetime | None = None,
) -> Handling:
    """Decide whether this case needs a person, and say why.

    ``require_precedent`` is the graduation switch: when on, a fix that
    has never cleared this case before is watched the first time. It is a
    deployment posture rather than a per-case setting, because "watch the
    first one" is a stance about the operator, not about the node.
    """
    from axiom.extensions.builtins.receipts.verdicts import verdicts_for

    condition = root_condition(case)
    remedy = condition.remedy
    summary = remedy.summary if remedy else ""
    runs = remedy.capability if remedy else ""

    history = verdicts_for(session, case.case_id, site=case.site)
    live = next((v for v in history if v.outcome is None), None)

    # A person already said leave it. Nothing reopens that but them.
    # FIRST, because a ceiling that other reasons could pre-empt is not a
    # ceiling. A reserved condition needs its person even when a fix is
    # declared, applies here, and has cleared this case a hundred times.
    if condition.authority_reserved:
        return Handling(
            fix_summary=summary,
            runs=runs,
            can_run=bool(remedy) and not (remedy.self_only and case.entity_id != this_node),
            needs_person=True,
            because=(
                f"This one is reserved to {condition.authority_reserved} — it does not "
                "become automatic however well it has gone before."
            ),
            reason=RESERVED,
        )

    if live is not None and live.chosen == "hold":
        return Handling(
            fix_summary=summary,
            runs=runs,
            can_run=False,
            needs_person=True,
            because="Someone chose to leave this alone.",
            reason=HELD,
        )

    if remedy is None:
        return Handling(
            fix_summary="",
            runs="",
            can_run=False,
            needs_person=True,
            because="No automatic fix is declared for this.",
            reason=NO_FIX,
        )

    if remedy.self_only and case.entity_id != this_node:
        return Handling(
            fix_summary=summary,
            runs=runs,
            can_run=False,
            needs_person=True,
            # Says what it MEANS for the reader, not only why they are
            # seeing it. The old sentence stopped at the reason — "only
            # node-a can put it right from its own side" — which leaves a
            # person asking "so what do I do?", and that question is the
            # whole thing a why-you sentence exists to answer. A reason
            # without its consequence is half a sentence.
            because=(
                f"Only {case.entity_id} can put this right from its own side, "
                "so nothing here can act on it — it needs somebody with access "
                "to that machine."
            ),
            reason=NOT_OURS,
        )

    # The fix ran, and the situation is still being reported. That is the
    # signal most worth a person's time, and it arrives with the evidence
    # that the obvious thing was already tried.
    if live is not None and live.chosen == "fix" and live.ran_ok is False:
        # It ran and reported it did nothing, or it failed outright. The
        # detail is the diagnosis, so it travels with the sentence rather
        # than being left in a column nobody reads.
        detail = (live.ran_detail or "").strip().rstrip(".")
        return Handling(
            fix_summary=summary,
            runs=runs,
            can_run=False,
            needs_person=True,
            because=(
                f"The fix ran and did not put it right: {detail}."
                if detail
                else "The fix ran and did not put it right."
            ),
            reason=DID_NOT_WORK,
        )

    if live is not None and live.chosen == "fix" and live.ran_ok:
        return Handling(
            fix_summary=summary,
            runs=runs,
            can_run=False,
            needs_person=True,
            because="The fix already ran and this is still happening.",
            reason=TRIED,
        )

    if require_precedent and not any(
        v.chosen == "fix" and v.ran_ok and v.outcome is not None for v in history
    ):
        return Handling(
            fix_summary=summary,
            runs=runs,
            can_run=True,
            needs_person=True,
            because="This fix has not cleared this before, so the first run is worth watching.",
            reason=NEVER_WORKED,
        )

    return Handling(
        fix_summary=summary,
        runs=runs,
        can_run=True,
        needs_person=False,
        because="",
    )


__all__ = [
    "DID_NOT_WORK",
    "HELD",
    "NEVER_WORKED",
    "NOT_OURS",
    "NO_FIX",
    "TRIED",
    "Handling",
    "handling_for",
    "root_condition",
]
