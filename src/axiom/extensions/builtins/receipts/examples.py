# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Two worked scenarios, kept side by side so the abstraction stays honest.

Founder direction (2026-09-26), after driving the surface against fixture
data and finding it unusable: *"map all this to a real world scenario
where somebody's trying to run a factory floor"*, and build **two** of
them, *"so that we're always keeping our eye on the abstraction between
them."*

Both are constructed illustrations. Neither describes a real site, and
the people in them are roles rather than anybody.

Why two, and why these two
--------------------------
One scenario proves nothing: any model fits a single example. Two unlike
ones are a test. These differ on the axis that matters — in the first an
agent **acts on the world** and reports what it did; in the second an
agent **asserts about data** and declares it fit to use. If one construct
expresses both without special cases, it is probably about supervision
rather than about machines.

The second is also deliberately the STRUCTURAL TWIN of the consumer case
this platform has to support: an agent conforming instrument readings and
declaring them valid. Same shape, different domain, so the mapping is
obvious without this module naming a consumer.

What every scenario must exercise
---------------------------------
The value of this construct over a conventional monitoring stack is
narrow and specific, and a scenario that does not exercise it is a worse
Nagios. Alert grouping is an incident; the cause chain is inhibition; a
hold is a silence with an expiry; a declared remedy is runbook
automation. All of that is decades old and good.

Three things are not:

1. **A claim asserted rather than checked.** Conventional monitoring has
   firing and resolved. It cannot distinguish *checked and held* from
   *asserted with no evidence that could be checked*, which is why a
   dashboard goes green when the checker dies — and why an agent that
   says "back in spec" is indistinguishable from a measurement.
2. **An outcome observed independently of whoever acted.** Ticketing
   records what you did; it never watches whether it worked.
3. **A record that can say whether this decider should be trusted with
   this condition next time.**

Every scenario here carries at least one ``unproven`` claim for the first,
a decidable case whose resolution can be observed for the second, and an
agent as the actor so the third has a subject.

Headlines lead with the CONSEQUENCE
-----------------------------------
The other founder finding from the same drive: *"I'm seeing information
that looks bad ... I'm not even really sure that I should be expected to
understand this low-level thing."* A stale claim is not "the reporter went
quiet" — that is a fact about a computer, and nobody's job. It is **"you
can no longer tell whether X is true"**, which is a fact about the
reader's own accountability and is actionable: check by hand, stop, or
accept running blind and say that you did.

So these scenarios' conditions are written consequence-first, with the
mechanism demoted to evidence. That is the altitude lift, stated as data
rather than as an argument.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from axiom.extensions.builtins.receipts.brief import OversightItem


@dataclass(frozen=True)
class Scenario:
    """One worked example: who is accountable, and what reached them."""

    key: str
    #: What the reader is accountable for, in their words. Not a job title.
    accountable_for: str
    #: One sentence a person outside the domain can follow.
    premise: str
    #: The agent under supervision — the reason this needs more than a
    #: threshold. Without one, a conventional stack is the right answer.
    agent: str
    items: tuple[OversightItem, ...]

    def oversight_items(self) -> list[OversightItem]:
        return list(self.items)


def _ago(hours: float, *, now: datetime | None = None) -> str:
    return ((now or datetime.now(UTC)) - timedelta(hours=hours)).isoformat()


def robot_fleet(*, now: datetime | None = None) -> Scenario:
    """A fleet of AI-enabled endpoints, supervised by an agent, answerable
    to a person. The founder's primary case (2026-09-26).

    What makes this different from any other fleet is narrow and worth
    stating, because most of fleet management is solved and this is not
    trying to redo it. Inventory, connectivity, version rollout, uptime —
    mature elsewhere.

    Three things are not:

    **The endpoint is the least reliable witness to its own correctness.**
    A pump that fails stops, and says so. A robot whose picking degrades
    keeps running, reports healthy, and emits plausible telemetry
    throughout. "Is it working" the endpoint can answer; "is it RIGHT" it
    structurally cannot. Only an outcome observed somewhere else can.

    **The unit of trust is (endpoint x condition), not the model.** Two
    hundred robots on byte-identical weights are not two hundred identical
    units: different bays mean different input distributions, so different
    reliability, at different tasks, at the same moment. A conventional
    fleet tool has no reason to model that — a van is a van.

    **A fix is simultaneous and global.** Replacing a bearing fixes one
    robot. Pushing weights changes the judgement of every endpoint at once
    with no per-unit trial, which makes a model update one decision with a
    fleet-wide reach rather than a deployment.

    And the supervisor here is itself an agent, so all three apply to the
    thing deciding which robots to trust. That is why authority is granted
    by a person and the grant is a record (spec §2, Solver / Decider) —
    the accountability chain has to survive a non-human link.
    """
    site = "dc-3"
    return Scenario(
        key="robot-fleet",
        accountable_for="the fleet doing its work correctly, and being able to say so",
        premise=(
            "Forty picking robots run the same on-board model. A fleet agent watches "
            "them, pulls endpoints it judges unreliable, and answers to the shift lead."
        ),
        agent="@fleet-warden:dc-3",
        items=(
            # THE differentiator, and the reason a conventional stack cannot
            # help: the endpoint is healthy by every measurable signal and
            # nothing has checked whether it is RIGHT.
            OversightItem(
                entity_kind="robot",
                entity_id="rbt-12",
                claim_kind="judgement",
                status="unproven",
                evidence=(
                    "rbt-12 has completed 1,430 picks since the model update. Every one "
                    "was accepted on its own confidence; none has been checked against "
                    "what was actually in the tote."
                ),
                site=site,
                observed_at=_ago(3.0, now=now),
            ),
            # Same fleet, same weights, a DIFFERENT endpoint failing at a
            # different task — the (endpoint x condition) point made as data
            # rather than as an argument.
            OversightItem(
                entity_kind="robot",
                entity_id="rbt-30",
                claim_kind="task_outcome",
                status="failed",
                evidence=(
                    "rbt-30 mislabelled 23 of its last 400 totes. rbt-12, on the same "
                    "weights, mislabelled none — the difference is the bay, not the "
                    "software."
                ),
                site=site,
                observed_at=_ago(1.0, now=now),
            ),
            # The supervisor is an agent too, and it has been acting on the
            # fleet. Nothing has checked whether its pulls were warranted.
            OversightItem(
                entity_kind="agent",
                entity_id="@fleet-warden:dc-3",
                claim_kind="calibration",
                status="unproven",
                evidence=(
                    "The fleet agent pulled 6 endpoints from service this week. None of "
                    "the 6 has been examined, so whether it was right to pull them is "
                    "not known — and it holds the authority to pull more."
                ),
                site=site,
                observed_at=_ago(5.0, now=now),
            ),
        ),
    )


def cold_chain(*, now: datetime | None = None) -> Scenario:
    """An agent that ASSERTS about data and declares it fit to use.

    Structurally the consumer case: readings arrive, an agent conforms
    them and marks them valid, and everything downstream believes it. The
    failures below are the shapes that have actually occurred — a sensor's
    fault word served as a measurement, and values served with no unit at
    all — both of which a conventional stack reports as healthy, because
    the pipeline is up and the rows are arriving.
    """
    site = "depot-west"
    return Scenario(
        key="cold-chain",
        accountable_for="every hold point staying met, and being able to prove it",
        premise=(
            "Freezer rooms report temperature every minute. An ingest agent conforms "
            "the readings and marks each one fit to use."
        ),
        agent="@conform:depot-west",
        items=(
            OversightItem(
                entity_kind="instrument",
                entity_id="room-b2",
                claim_kind="reading_validity",
                status="unproven",
                evidence=(
                    "The conform agent marked 4,812 readings valid in the last day. "
                    "3,109 of them carry no unit, so 'valid' cannot mean within range."
                ),
                site=site,
                observed_at=_ago(1.0, now=now),
            ),
            OversightItem(
                entity_kind="instrument",
                entity_id="room-b2",
                claim_kind="hold_point",
                status="failed",
                evidence=(
                    "21% of yesterday's readings for room-b2 are exactly 3276.75, which "
                    "is the probe's fault word served as a temperature."
                ),
                site=site,
                observed_at=_ago(6.0, now=now),
            ),
            OversightItem(
                entity_kind="instrument",
                entity_id="room-c1",
                claim_kind="reading_link",
                status="stale",
                evidence=("Nothing has arrived from room-c1 for 2 hours. It reports every minute."),
                site=site,
                observed_at=_ago(2.0, now=now),
            ),
        ),
    )


def all_scenarios(*, now: datetime | None = None) -> dict[str, Scenario]:
    """Both, keyed — so a caller cannot reach for one without the other."""
    return {s.key: s for s in (robot_fleet(now=now), cold_chain(now=now))}


__all__ = ["Scenario", "all_scenarios", "cold_chain", "robot_fleet"]
