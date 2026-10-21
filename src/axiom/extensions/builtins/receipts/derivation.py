# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""How a claim was arrived at — as DATA, not as a view.

Founder (2026-09-25): "how do we prevent this type of problem where what
the LLM is saying is not congruent with reality ... somehow we need to
provide a window into that kind of resolution, not just fix blindly."
And, asked which shape that window should take: "I don't know enough
about how this will be consumed to be opinionated so be flexible."

So it is not a screen. It is a structure attached to each claim, and the
renderings are projections of it — the same rule the rest of this
surface follows. A per-claim drill-down, a whole-case audit page, the
CLI, the courier and a PDF for somebody who was not in the room all read
the same payload, and none of them can say something the others cannot.

This is not hypothetical caution. The first time a fix ran here the
record said ``ran_ok: true`` while its own detail said "node is not
enrolled — nothing sent". The button had run something, the something
had done nothing, and the record said success. It was caught by reading
the raw payload by hand.

A derivation therefore carries four things, and the last two are the
ones that matter:

- ``inputs`` — the stored values, so the arithmetic can be redone.
- ``rule`` — what was applied to them, in one sentence.
- ``verify`` — a command that checks the claim WITHOUT this platform.
  Not "trust the receipt": go and look. A claim with no independent
  check says so rather than implying one exists.
- ``limit`` — what this cannot establish. "Nothing arrived" is not "the
  node is down", and the distinction is the same one UNPROVEN draws
  against FAILED. A window that hides it would be worse than no window,
  because it would make a derived claim look like an observed one.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Input:
    """One stored value a claim was computed from."""

    label: str
    value: str

    def payload(self) -> dict:
        return {"label": self.label, "value": self.value}


@dataclass(frozen=True)
class Derivation:
    """How one claim was arrived at, and how to check it independently."""

    #: what this explains — "claim:heartbeat", "cause", "reach", "fix".
    #: A flat, named list is what lets one payload serve a per-claim
    #: drill-down AND a whole-case audit without either shape winning.
    subject: str
    #: the sentence the surface shows, repeated here so a projection that
    #: renders derivations alone is still readable.
    claim: str
    inputs: list[Input] = field(default_factory=list)
    rule: str = ""
    #: a command that checks this without believing us. "" = none exists,
    #: which is stated rather than implied away.
    verify: str = ""
    #: what this cannot establish.
    limit: str = ""

    def payload(self) -> dict:
        return {
            "subject": self.subject,
            "claim": self.claim,
            "inputs": [i.payload() for i in self.inputs],
            "rule": self.rule,
            "verify": self.verify,
            "limit": self.limit,
        }


def stale_derivation(
    *,
    claim_kind: str,
    entity_id: str,
    claim: str,
    received_at: str,
    age_seconds: int,
    cadence_seconds: int,
    multiplier: int,
) -> Derivation:
    """The staleness judgement, shown as arithmetic.

    This is the most-read claim on the surface and the one with real
    numbers behind it, so it is the one most worth being able to redo.
    """
    return Derivation(
        subject=f"claim:{claim_kind}",
        claim=claim,
        inputs=[
            Input(label="Last report stored at", value=received_at),
            Input(label="Age now", value=f"{age_seconds} seconds"),
            Input(label="Declared cadence", value=f"{cadence_seconds} seconds"),
            Input(label="Stale after", value=f"{multiplier} x cadence"),
        ],
        rule=(
            f"{age_seconds}s is more than {multiplier} x {cadence_seconds}s, "
            "so the report is stale."
        ),
        verify=f"axi fleet status --node {entity_id}",
        limit=(
            "This establishes that nothing arrived, not that the node is down. "
            "A healthy node with a broken reporter looks the same from here."
        ),
    )


def cause_derivation(root_kind: str, explained: list[str]) -> Derivation:
    """Why several claims were collapsed into one problem.

    The collapse is a RULE, and a rule the reader cannot see is a
    judgement they are being asked to take on faith.
    """
    return Derivation(
        subject="cause",
        claim=f"One problem, not {len(explained) + 1}.",
        inputs=[
            Input(label="Root", value=root_kind.replace("_", " ")),
            Input(label="Explained by it", value=", ".join(k.replace("_", " ") for k in explained)),
        ],
        rule=(
            "A reporter that has gone quiet cannot deliver anything else either, "
            "so other claims that are ALSO merely silent are the same fault. "
            "A claim that is failed or unevidenced is never collapsed this way, "
            "because its report did arrive and its content was wrong."
        ),
        verify="",
        limit=(
            "This groups claims that share a cause. It does not rule out a second "
            "fault hiding behind the first, which is why only silence is collapsed."
        ),
    )


def reach_derivation(summary: str, sources: list[str]) -> Derivation:
    """Reach, with the declarations it was counted from."""
    return Derivation(
        subject="reach",
        claim=summary,
        inputs=[Input(label="Counted from", value=s) for s in sources],
        rule="Only edges something actually declared are counted.",
        verify="",
        limit=(
            "This is a fact about the declarations, not about the world. "
            "Anything that depends on this without declaring so is not here."
        ),
    )


def fix_derivation(*, capability: str, ok: bool | None, detail: str, before: str) -> Derivation:
    """What a fix actually did — the claim most worth distrusting.

    The first fix that ran here reported success while doing nothing.
    ``ok`` is what the platform concluded; ``detail`` is what the
    capability itself said, kept verbatim so the two can be compared by
    whoever reads this rather than only by whoever wrote it.
    """
    return Derivation(
        subject="fix",
        claim=("The fix ran." if ok else "The fix did not put it right."),
        inputs=[
            Input(label="Ran", value=capability or "nothing"),
            Input(label="It reported", value=detail or "nothing"),
            Input(label="Before it ran", value=before),
        ],
        rule=(
            "A capability that exits without error but reports doing nothing is "
            "not a fix. Where a remedy declares how to tell, that is checked "
            "rather than assumed from the exit."
        ),
        verify="",
        limit=(
            "This says what the fix reported, not that the situation is better. "
            "That only arrives when the evaluator stops reporting the case."
        ),
    )


__all__ = [
    "Derivation",
    "Input",
    "cause_derivation",
    "fix_derivation",
    "reach_derivation",
    "stale_derivation",
]
