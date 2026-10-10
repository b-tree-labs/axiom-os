# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One cause, not N problems — the case's story, derived.

A node with three bad claims usually has ONE thing wrong. Shown as
three rows it costs three investigations; shown as a chain it costs
one. That difference is the whole point of a case.

The rules here are narrow and each is defensible from the evaluator's
own output — this module NEVER guesses at causality:

- A stale heartbeat means the reporter stopped pushing. Every other
  claim that is *stale* on that node is then stale FOR THAT REASON:
  nothing is arriving, so nothing can be fresh. Chasing them
  separately is chasing one fault three times.
- A claim that is FAILED or UNPROVEN is NOT explained by silence: the
  report arrived and its content was wrong or unevidenced. Those stay
  independent, because collapsing them would hide a real second fault.

Anything the rules cannot explain is a root — and a case with several
roots says so, rather than inventing a single story.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from axiom.extensions.builtins.receipts.brief import OversightItem
from axiom.extensions.builtins.receipts.conditions import condition_for

#: The claim whose silence explains other silence on the same entity.
_REPORTER_CLAIM = "heartbeat"

#: Statuses that mean "nothing arrived" (explainable by a dead reporter)
#: versus statuses that mean "something arrived and was wrong" (not).
_SILENCE = {"stale", "unknown"}


@dataclass(frozen=True)
class CauseStep:
    """One line of the story: a claim, and why it is where it is."""

    claim_kind: str
    status: str
    evidence: str
    observed_at: str = ""
    #: the claim that accounts for this one, if any
    explained_by: str | None = None
    #: why, in the surface's own words
    because: str = ""
    #: what this step MEANS, as a sentence. The surface should not have
    #: to assemble "heartbeat is stale" out of two of our field names.
    reads: str = ""

    @property
    def is_root(self) -> bool:
        return self.explained_by is None

    def payload(self) -> dict:
        return {
            "claim_kind": self.claim_kind,
            "status": self.status,
            "evidence": self.evidence,
            "observed_at": self.observed_at,
            "explained_by": self.explained_by,
            "because": self.because,
            "reads": self.reads,
            "is_root": self.is_root,
        }


@dataclass(frozen=True)
class CauseChain:
    steps: list[CauseStep] = field(default_factory=list)

    @property
    def roots(self) -> list[CauseStep]:
        return [s for s in self.steps if s.is_root]

    def summary(self) -> str:
        """The first sentence a person reads about this case.

        Said in their words, not ours: what is wrong with their thing,
        and whether it is one thing or several. No field names, no
        status enums, no counting of "claims".
        """
        roots = self.roots
        if not self.steps:
            return ""
        if len(roots) == 1:
            lone = roots[0].reads
            if len(self.steps) == 1:
                return f"{lone}."
            return f"One problem, not {len(self.steps)}. {lone} — the rest follows from that."
        return (
            f"{len(roots)} separate problems — "
            + "; ".join(r.reads for r in roots)
            + ". Fixing one will not fix the other."
        )

    def payload(self) -> dict:
        return {"steps": [s.payload() for s in self.steps], "summary": self.summary()}


def explain(items: list[OversightItem]) -> CauseChain:
    """Order a case's claims into a chain, marking what explains what."""
    if not items:
        return CauseChain()

    by_kind = {i.claim_kind: i for i in items}
    reporter = by_kind.get(_REPORTER_CLAIM)
    reporter_is_silent = reporter is not None and reporter.status in _SILENCE

    steps: list[CauseStep] = []
    for item in items:
        explained_by: str | None = None
        because = ""
        if reporter_is_silent and item.claim_kind != _REPORTER_CLAIM and item.status in _SILENCE:
            explained_by = _REPORTER_CLAIM
            because = (
                "nothing is arriving from this node, so this cannot be "
                "current — it is the same problem, not a second one"
            )
        steps.append(
            CauseStep(
                claim_kind=item.claim_kind,
                status=item.status,
                evidence=item.evidence,
                observed_at=item.observed_at,
                explained_by=explained_by,
                because=because,
                reads=condition_for(item.claim_kind, item.status).title_for(item.entity_id),
            )
        )

    # Roots first (the thing to fix), then what they account for. Within
    # each group the order is the evaluator's, which is already total.
    steps.sort(key=lambda s: (not s.is_root,))
    return CauseChain(steps=steps)


__all__ = ["CauseChain", "CauseStep", "explain"]
