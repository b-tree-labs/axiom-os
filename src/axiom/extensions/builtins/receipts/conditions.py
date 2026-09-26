# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What a condition MEANS, in the reader's words.

Founder feedback (2026-09-24): the surface read "too low level for the
average developer even ... very idiomatic". It was describing our data
model — claims, cadences, status enums — and pasting shell runbooks into
sentences. A person opening a case wants to know what happened to their
thing and what to do about it.

So a condition — one ``(claim_kind, status)`` pair — carries three
things, and every one of them is a sentence:

- ``headline`` completes the case's title. ``{entity}`` is substituted,
  which lets a condition read either as a continuation ("node-a stopped
  reporting") or as a statement ("node-a: the backup did not work")
  without a flag deciding which.
- ``detail`` says what the surface actually knows, without timestamps to
  the microsecond or raw second counts.
- ``fix`` says what would put it right, in plain words. No shell. A
  command a person must retype is a runbook, not a fix, and the runnable
  form belongs in the remedy that rides alongside.

A condition nobody has written yet still has to read as English, so the
fallbacks below are phrased per status rather than dumping the fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Remedy:
    """A fix the platform can actually carry out.

    ``capability`` names a REGISTERED skill, not a shell line — a guard
    test fails the build if it names one that does not exist, because a
    button for a capability nobody registered is the worst kind of dead
    affordance: it looks like the product works.

    ``self_only`` is the honest half. ``fleet.report`` makes THIS node
    report; it does nothing whatsoever for a different node that has
    gone quiet. A console cannot restart a reporter on somebody else's
    machine, and offering to would be a lie told with a button. When a
    remedy is self-only and the case is about another entity, the case
    says so and offers nothing.
    """

    capability: str
    params: dict = field(default_factory=dict)
    summary: str = ""
    self_only: bool = True
    #: A key in the capability's ``SkillResult.value`` that must be truthy
    #: for the run to count as having DONE anything. Probe-shaped skills
    #: exit ok when they deliberately do nothing — ``fleet.report`` on an
    #: unenrolled node returns ok with ``{"enrolled": False}``, because a
    #: probe never warns. Reading that as a successful fix would tell a
    #: person the thing was handled when nothing happened at all.
    proves_done: str = ""


@dataclass(frozen=True)
class Condition:
    """One ``(claim_kind, status)`` in plain language."""

    headline: str
    detail: str
    fix: str
    #: what the platform could RUN to put it right. None = nobody has
    #: declared one, which the surface states rather than implying that
    #: no fix exists in the world.
    remedy: Remedy | None = None

    def title_for(self, entity_id: str) -> str:
        return self.headline.format(entity=entity_id)


def words(claim_kind: str) -> str:
    """``service_health`` reads as ``service health``. Our field names are
    not the reader's vocabulary."""
    return claim_kind.replace("_", " ")


#: The conditions we have phrased. Keyed exactly like the remedy table,
#: because they answer two halves of the same question.
CONDITIONS: dict[tuple[str, str], Condition] = {
    ("heartbeat", "stale"): Condition(
        headline="{entity} stopped reporting",
        detail="Nothing has arrived from this node, so nothing else about it can be current.",
        fix="Make this node report now.",
        # fleet.report is the node-side push: it reports about the node it
        # runs on. That fixes this node going quiet and does nothing at all
        # for another node that has, hence self_only.
        remedy=Remedy(
            capability="fleet.report",
            summary="Make this node report now.",
            self_only=True,
            # "sent" is how many reports actually went out. Absent or zero
            # means the push never happened.
            proves_done="sent",
        ),
    ),
    ("service_health", "stale"): Condition(
        headline="{entity}: the health report is out of date",
        detail="The node's health report has not been refreshed.",
        fix="Make this node report now.",
        remedy=Remedy(
            capability="fleet.report",
            summary="Make this node report now.",
            self_only=True,
            proves_done="sent",
        ),
    ),
    ("service_health", "unproven"): Condition(
        headline="{entity}: health is claimed but not measured",
        detail="The node says its services are healthy without reporting how long they took.",
        fix="Have the node report a response time for each service.",
    ),
    ("backup", "stale"): Condition(
        headline="{entity}: no recent backup",
        detail="No backup has been reported within the window this node promises.",
        fix="Run a backup on this node.",
    ),
    ("backup", "unproven"): Condition(
        headline="{entity}: a backup is claimed with nothing to show for it",
        detail=(
            "The node reported a backup but named no file, size or time, "
            "so there is nothing to check."
        ),
        fix="Run a backup that records the file it wrote.",
    ),
    ("backup", "failed"): Condition(
        headline="{entity}: the backup did not work",
        detail="The evidence contradicts the backup the node claimed.",
        fix="Run a backup and verify what it wrote.",
    ),
    ("canary", "stale"): Condition(
        headline="{entity}: not checking for updates",
        detail=(
            "The update check has not run, so this node could be missing "
            "updates without anything saying so."
        ),
        fix="Restart the update check on this node.",
    ),
}

#: How an unphrased condition reads. Each is a sentence, because the
#: alternative — printing the field names — is what the feedback was about.
_FALLBACK_HEADLINE = {
    "failed": "{entity}: the {kind} check failed",
    "stale": "{entity}: {kind} has not been checked recently",
    "unproven": "{entity}: {kind} is claimed with no evidence",
    "unknown": "{entity}: {kind} is unknown",
}

_FALLBACK_DETAIL = {
    "failed": "What was reported contradicts the {kind} claim.",
    "stale": "No recent {kind} report has arrived.",
    "unproven": "{kind} was claimed without evidence to check.",
    "unknown": "Nothing has said what the {kind} state is.",
}


def condition_for(claim_kind: str, status: str) -> Condition:
    """The plain-language reading of a condition. Always answers."""
    known = CONDITIONS.get((claim_kind, status))
    if known is not None:
        return known
    kind = words(claim_kind)
    headline = _FALLBACK_HEADLINE.get(status, "{entity}: {kind} needs a look")
    detail = _FALLBACK_DETAIL.get(status, "The {kind} check needs a look.")
    return Condition(
        headline=headline.replace("{kind}", kind),
        detail=detail.replace("{kind}", kind),
        # No invented fix: a condition nobody has phrased is one nobody
        # has written a fix for either, and saying otherwise would be
        # putting words in the platform's mouth.
        fix="",
    )


__all__ = ["CONDITIONS", "Condition", "Remedy", "condition_for", "words"]
