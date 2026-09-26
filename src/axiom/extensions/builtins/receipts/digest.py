# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The arrival stage: what reaches a person who is not looking at this.

The journey map (docs/working/oversight-journey-map-2026-09-25.md) scored
arrival 9 and found nothing in it. Every other stage was gated on a stage
that did not exist: a well-built decision surface a person reaches only
if they happen to open it.

This is a PROJECTION of the brief, not a second composer. It renders
``Brief`` — the same object the web payload, the CLI text and the MCP
courier render — so the digest cannot say something the surface does not,
and a change to what a case means reaches all four at once.

Three rules, each of them the construct's rather than this module's:

**It is a digest, not a per-case ping.** The whole construct is built
against alert fatigue. One message describing the day is one
interruption; one message per case is how people stop reading.

**It sends on the cadence even when quiet.** A digest that arrives only
when something is wrong makes "nothing is wrong" indistinguishable from
"the digest is broken", which is the liveness problem wearing a
different hat. The quiet form is two lines.

**It says why, not what.** A case's evidence, reach and derivation live
on the surface; carrying them here would make a message nobody can read
and a second place for them to drift. The digest carries the headline
and the one sentence that says why a person is needed, and a link.
"""

from __future__ import annotations

from dataclasses import dataclass

from axiom.extensions.builtins.receipts.brief import Brief

#: The line a quiet digest ends on. Present so the reader can tell a
#: quiet day from a broken sender: silence is never the all-clear.
ALL_CLEAR = "Nothing is waiting on you."


@dataclass(frozen=True)
class Digest:
    """What arrives, and whether it was worth arriving."""

    subject: str
    body: str
    #: how many cases are waiting on a person
    waiting: int
    #: False when this is the periodic all-clear rather than a demand.
    #: Callers may route the two differently; they must still send both.
    needs_anyone: bool

    def payload(self) -> dict:
        return {
            "subject": self.subject,
            "body": self.body,
            "waiting": self.waiting,
            "needs_anyone": self.needs_anyone,
        }


def compose_digest(brief: Brief, *, where: str = "") -> Digest:
    """Render a brief as the message that arrives.

    ``where`` is the link to the surface. Omitted rather than faked when
    a deployment has not told us its address — a digest that points
    somewhere wrong is worse than one that points nowhere.
    """
    cases = list(brief.cases)
    waiting = int(brief.counts.get("cases", len(cases)))
    site = f" · {brief.site}" if brief.site else ""

    if waiting == 0:
        subject = f"Nothing waiting{site}"
        lines = [ALL_CLEAR, brief.quiet_line]
        if where:
            lines.append(where)
        return Digest(
            subject=subject,
            body="\n".join(line for line in lines if line),
            waiting=0,
            needs_anyone=False,
        )

    thing = "case" if waiting == 1 else "cases"
    subject = f"{waiting} {thing} waiting on you{site}"

    lines: list[str] = []
    for case in cases:
        lines.append(f"• {case.title}")
        # Why a person is needed — composed by the server
        # (receipts.handling), never reworded here.
        handling = case.handling or {}
        because = handling.get("because") or ""
        if because:
            lines.append(f"  {because}")
        elif handling.get("can_run") and handling.get("fix_summary"):
            # Nothing is stopping this one being handled; it is waiting on
            # somebody to press the button, which is a different thing from
            # needing their judgement and should not read the same.
            summary = str(handling["fix_summary"]).rstrip(".")
            lines.append(f"  A fix is ready: {summary[0].lower() + summary[1:]}.")

    hidden = waiting - len(cases)
    if hidden > 0:
        # The cap is on what is shown, never on what is counted.
        lines.append(f"• {hidden} more not shown — the list is capped on purpose")

    if brief.quiet_line:
        lines.append("")
        lines.append(brief.quiet_line)
    if where:
        lines.append(where)

    return Digest(
        subject=subject,
        body="\n".join(lines),
        waiting=waiting,
        needs_anyone=True,
    )


__all__ = ["ALL_CLEAR", "Digest", "compose_digest"]
