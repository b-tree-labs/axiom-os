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
from datetime import UTC, datetime

from axiom.extensions.builtins.receipts.brief import Brief

#: The line a quiet digest ends on. Present so the reader can tell a
#: quiet day from a broken sender: silence is never the all-clear.
ALL_CLEAR = "Nothing is waiting on you."

#: What a case says when nothing it carries can date it. Absence is
#: written rather than left blank, because a case with no age silently
#: reads as a fresh one — and a fresh case is the one you are NOT late on.
AGE_UNKNOWN = "how long is not recorded"


def _age(observed_at: str, *, now: datetime) -> str:
    """How long the condition has held, from the claim's own timestamp.

    Deliberately the age of the CONDITION, not of the case. Cases are
    recomputed from current state on every poll (spec §3, ``observe``), so
    a case has no memory of when it first appeared and nothing persists
    one. What the claims do carry is when the evidence was last received,
    which for a silent reporter is when the silence began.

    The limit is worth stating where a reader will see it: this says how
    long the situation has been true, not how long a person has been
    ignoring it. Those diverge after a hold lapses, and the first is the
    number that answers "am I late".
    """
    if not observed_at:
        return AGE_UNKNOWN
    try:
        seen = datetime.fromisoformat(observed_at)
    except ValueError:
        return AGE_UNKNOWN
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=UTC)
    seconds = (now - seen).total_seconds()
    if seconds < 0:
        # A clock disagreement, not a fact about the case. Say nothing
        # rather than "in 3 minutes".
        return AGE_UNKNOWN
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            n = int(seconds // size)
            return f"{n} {unit}{'s' if n != 1 else ''}"
    return "under a minute"


def _site_suffix(brief: Brief, cases: list) -> str:
    """ " · site" when this digest is about exactly one, else "".

    Derived from the CASES rather than from whatever scope the caller asked
    for. A run scoped to one site and a run that merely happens to find one
    site's cases are the same message to the reader, and a request scope is
    a statement about the query rather than about what arrived.

    Silent on a mix, because naming one site would be naming the wrong one —
    and silent with no cases, where the all-clear still carries the
    requested scope if there was one.
    """
    sites = {str(getattr(c, "site", "") or "") for c in cases}
    sites.discard("")
    if len(sites) == 1:
        return f" · {sites.pop()}"
    if not sites and brief.site:
        return f" · {brief.site}"
    return ""


def _oldest_claim(case) -> str:
    """The earliest timestamp among a case's claims, or "".

    The oldest claim dates the situation. A node whose heartbeat went
    quiet two hours ago and whose service check went unproven one minute
    ago has been in trouble for two hours.
    """
    stamps = [str(getattr(i, "observed_at", "") or "") for i in getattr(case, "items", [])]
    stamps = [s for s in stamps if s]
    return min(stamps) if stamps else ""


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


def compose_digest(brief: Brief, *, where: str = "", now: datetime | None = None) -> Digest:
    """Render a brief as the message that arrives.

    ``where`` is the link to the surface. Omitted rather than faked when
    a deployment has not told us its address — a digest that points
    somewhere wrong is worse than one that points nowhere.

    ``now`` is injected so the ages below are not read off a clock the
    test cannot control; it defaults to the real one.
    """
    at = now or datetime.now(UTC)
    cases = list(brief.cases)
    waiting = int(brief.counts.get("cases", len(cases)))
    site = _site_suffix(brief, cases)

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
        # The age rides the headline rather than taking a line of its own.
        # "For 2 hours" is the difference between a case somebody should
        # look at and one somebody is already late on, and a reader
        # skimming a phone will not scroll for it.
        age = _age(_oldest_claim(case), now=at)
        prefix = age if age == AGE_UNKNOWN else f"for {age}"
        lines.append(f"• {case.title} — {prefix}")
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


__all__ = ["AGE_UNKNOWN", "ALL_CLEAR", "Digest", "compose_digest"]
