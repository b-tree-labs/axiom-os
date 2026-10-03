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
    #: Who must decide this, when NO track record can confer the right to
    #: act unattended. Empty = autonomy is earnable in the usual way.
    #:
    #: Everything else in this model treats autonomy as EARNED: a fix that
    #: has cleared a case before may run unheeded, which is the gray area
    #: measured instead of adjudicated (see handling.NEVER_WORKED). Some
    #: decisions are not like that. A thousand correct calls do not confer
    #: authority that is personal and non-delegable, and for a decision
    #: whose reach is a whole fleet, a good track record on single units is
    #: not evidence about the fleet-wide act at all.
    #:
    #: So this is a CEILING, not a threshold — declared per condition,
    #: reviewable in one table, and never computed. It outranks every other
    #: handling reason, because a reason that could be argued past is not a
    #: ceiling.
    authority_reserved: str = ""

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
    # --- The worked scenarios (receipts.examples) ------------------------
    #
    # Written CONSEQUENCE-FIRST, which is the point of them. Founder,
    # 2026-09-26, after driving the surface: "I'm not even really sure that
    # I should be expected to understand this low-level thing that's
    # reporting a problem." A headline that names the mechanism hands the
    # reader somebody else's job. A headline that names what they can no
    # longer rely on hands them their own.
    #
    # The mechanism is not lost — it is the evidence, one line down, where
    # a person who wants it can check the claim instead of taking it.
    ("judgement", "unproven"): Condition(
        headline="{entity}: working, and nobody has checked whether it is right",
        detail=(
            "It reports healthy and is completing work. Every result was accepted on "
            "its own confidence, and nothing independent has looked at one — so this "
            "is not a machine that might be broken, it is work that might be wrong."
        ),
        fix="Check a sample of its recent work against what was actually there.",
    ),
    ("task_outcome", "failed"): Condition(
        headline="{entity}: getting it wrong often enough to matter",
        detail=(
            "Checked work from this endpoint is coming back wrong at a rate the fleet "
            "does not allow for. Other endpoints on the same software are not, so this "
            "is about where it is working rather than what it is running."
        ),
        fix="Take it out of service and look at what it is seeing.",
    ),
    ("calibration", "unproven"): Condition(
        headline="{entity}: acting on the fleet, with nothing checking its judgement",
        detail=(
            "It holds the authority to pull endpoints from service and has been using "
            "it. None of those calls has been examined, so whether it is exercising "
            "that authority well is not known — and it keeps the authority meanwhile."
        ),
        fix="Review what it pulled and whether each one deserved it.",
        # The ceiling, demonstrated where it matters most: judging whether a
        # supervising agent is exercising its authority well is not a thing
        # that agent — or any agent — can be trusted to conclude about
        # itself, however good its record. Phrased by role rather than by
        # title, because this table is domain-agnostic.
        authority_reserved="the person accountable for the fleet",
    ),
    # --- Disparate autonomous equipment under one operator ---------------
    # Written because the fallbacks ("the valve state check failed") speak
    # our data model, which is the whole complaint. Note what these three
    # expose: all are `failed`, and they are not the same problem at all.
    ("valve_state", "failed"): Condition(
        headline="{entity}: watering past its window, right now",
        detail=(
            "It has reported its valve open well past the schedule. If that is true "
            "the field is being over-watered while this sits here."
        ),
        fix="Close the zone, then find out why it stayed open.",
    ),
    ("field_work", "failed"): Condition(
        headline="{entity}: left gaps in work already recorded as done",
        detail=(
            "The pass finished and was logged complete, but rows were missed. Nothing "
            "will catch them until the crop shows it, by which point the window to "
            "redo the pass has gone."
        ),
        fix="Mark the block for a second pass before conditions close it out.",
    ),
    ("detection", "failed"): Condition(
        headline="{entity}: raising alarms that are usually wrong",
        detail=(
            "Most of what it flagged overnight was reviewed and was not what it said. "
            "An alarm that is usually wrong stops being answered, which costs more "
            "than the alarm."
        ),
        fix="Take it out of the alerting set until it has been retuned.",
    ),
    ("reading_validity", "unproven"): Condition(
        headline="{entity}: readings are marked usable without saying what they mean",
        detail=(
            "The agent marked them fit to use, but a value with no unit is not a "
            "measurement — nothing downstream can tell whether a number is inside a "
            "limit or outside it."
        ),
        fix="Declare the unit for this instrument, then re-run the check.",
    ),
    ("hold_point", "failed"): Condition(
        headline="{entity}: a fault code is being served as a real reading",
        detail=(
            "A repeated value matching the probe's fault word is being carried "
            "downstream as a measurement, so anything averaging these readings is "
            "quietly wrong rather than obviously missing."
        ),
        fix="Quarantine the affected readings and declare the fault code.",
    ),
    ("reading_link", "stale"): Condition(
        headline="{entity}: you can no longer tell whether it is being held",
        detail=(
            "Nothing has arrived for longer than its reporting interval. The "
            "conditions inside are not known to be wrong — they are not known."
        ),
        fix="Check it in person, or record that you are accepting the gap.",
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
