# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A fleet with a history, so the thing we claim to be good at has data.

The worked scenarios in ``receipts.examples`` are a snapshot: three claims,
one moment. That is enough to argue about wording and not enough to
demonstrate anything, because the whole differentiating claim of this
construct is about a record accumulating over time — precedent, observed
outcomes, and whether a given decider should be trusted with a given
condition next time. A snapshot can show none of that.

So this builds the same scenarios at fleet scale with weeks behind them.

What realism means here, and why it matters
-------------------------------------------
The single most important property is the MIX, not the count. In a fleet
of AI-enabled endpoints the dominant state is not "broken" and not
"healthy" — it is **unverified**. Most endpoints are working, reporting
fine, and have had none of their actual work checked by anything other
than themselves.

That is the demo in one line: a conventional dashboard shows forty green,
and it is not lying — every endpoint is up, every heartbeat is on time.
This surface shows that thirty-one of them are unverified, three are
getting it wrong, and the agent supervising them has been pulling units
from service with nobody checking whether it should have.

So the generator deliberately produces a long `unproven` tail. A fixture
where everything is either fine or failing would be a fixture for a
monitoring tool, and would quietly train us to build one.

Determinism
-----------
Seeded and pure: the same seed yields the same fleet and the same history,
so a brief composed from it is byte-stable and a test can assert on the
words. Nothing here reads a clock except through the ``now`` it is given.

Scenario-driven, not robot-specific
-----------------------------------
The generator takes its vocabulary from the scenario rather than knowing
about robots, which is the same abstraction check the two scenarios exist
for: if the cold-chain fleet needs a second generator, the construct was
about robots after all.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from axiom.extensions.builtins.receipts.brief import OversightItem

#: The mix, as fractions of a fleet. Deliberately unproven-dominant — see
#: the module docstring. These are the shape of the problem, not tuning
#: knobs: a fixture without a long unverified tail cannot show what this
#: construct is for.
HEALTHY = 0.15
UNVERIFIED = 0.70
DEGRADED = 0.10
WRONG = 0.05


@dataclass(frozen=True)
class Population:
    """What a scenario's fleet is made of, in the scenario's own words."""

    entity_kind: str
    #: ``prefix-NN`` ids.
    prefix: str
    size: int
    #: The claim kind that says "working, and unchecked".
    unverified_claim: str
    #: The claim kind that says "checked, and coming back wrong".
    wrong_claim: str
    #: The claim kind that says "we can no longer tell".
    degraded_claim: str
    #: Phrases with one ``{n}``/``{id}`` slot, so evidence reads like the
    #: domain rather than like a template.
    unverified_evidence: str
    wrong_evidence: str
    degraded_evidence: str


@dataclass(frozen=True)
class Decision:
    """One past decision, with what actually happened afterwards.

    ``cleared`` is the half nothing else records: whether the situation
    went away, observed rather than reported by whoever decided. That is
    the column calibration is computed from, and the reason a ticket
    history cannot answer the same question.
    """

    entity_id: str
    claim_kind: str
    chosen: str
    decider: str
    decider_kind: str
    at: datetime
    cleared: bool


@dataclass(frozen=True)
class SimulatedFleet:
    """A fleet at one moment, with the record that got it there."""

    site: str
    items: tuple[OversightItem, ...]
    history: tuple[Decision, ...] = field(default_factory=tuple)

    def oversight_items(self) -> list[OversightItem]:
        return list(self.items)

    def counts_by_status(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for i in self.items:
            out[i.status] = out.get(i.status, 0) + 1
        return out

    def calibration_of(self, decider: str) -> tuple[int, int]:
        """(cleared, total) for one decider — the arithmetic, by hand.

        Here only so a fixture can be checked against it. The real thing is
        `calibrate` (spec-case-construct §3), which is not built yet; this
        exists so that when it is, there is data to run it on and a known
        answer to compare against.
        """
        mine = [d for d in self.history if d.decider == decider]
        return sum(1 for d in mine if d.cleared), len(mine)


def _ids(prefix: str, size: int) -> list[str]:
    return [f"{prefix}-{n:02d}" for n in range(1, size + 1)]


def simulate(
    population: Population,
    *,
    site: str,
    now: datetime,
    seed: int = 7,
    weeks: int = 3,
    deciders: tuple[tuple[str, str, float], ...] = (),
) -> SimulatedFleet:
    """Build a fleet and the weeks behind it.

    ``deciders`` is ``(principal, kind, clear_rate)``. The rates differ on
    purpose: a fixture where every decider is equally good cannot show the
    one thing the record is for. Nothing in the surface reads these — they
    are the ground truth a calibration measurement should be able to
    RECOVER from the record, which is how we will know the measurement
    works rather than merely runs.
    """
    rng = random.Random(seed)
    ids = _ids(population.prefix, population.size)
    rng.shuffle(ids)

    n_wrong = max(1, round(population.size * WRONG))
    n_degraded = max(1, round(population.size * DEGRADED))
    n_unverified = max(1, round(population.size * UNVERIFIED))

    wrong = ids[:n_wrong]
    degraded = ids[n_wrong : n_wrong + n_degraded]
    unverified = ids[n_wrong + n_degraded : n_wrong + n_degraded + n_unverified]
    # The remainder is healthy: checked recently, and it held. They produce
    # `green` claims, which is what makes the quiet line honest — a brief
    # that only ever sees trouble cannot say what passed.
    healthy = ids[n_wrong + n_degraded + n_unverified :]

    items: list[OversightItem] = []

    def _add(entity: str, kind: str, status: str, evidence: str, hours_ago: float) -> None:
        items.append(
            OversightItem(
                entity_kind=population.entity_kind,
                entity_id=entity,
                claim_kind=kind,
                status=status,
                evidence=evidence,
                site=site,
                observed_at=(now - timedelta(hours=hours_ago)).isoformat(),
            )
        )

    for e in wrong:
        bad = rng.randint(11, 40)
        _add(
            e,
            population.wrong_claim,
            "failed",
            population.wrong_evidence.format(id=e, n=bad),
            rng.uniform(0.5, 6),
        )
    for e in degraded:
        _add(
            e,
            population.degraded_claim,
            "stale",
            population.degraded_evidence.format(id=e, n=rng.randint(2, 9)),
            rng.uniform(2, 30),
        )
    for e in unverified:
        _add(
            e,
            population.unverified_claim,
            "unproven",
            population.unverified_evidence.format(id=e, n=rng.randint(200, 2400)),
            rng.uniform(1, 12),
        )
    for e in healthy:
        _add(
            e,
            population.unverified_claim,
            "green",
            f"{e}: a sample of its recent work was checked and held.",
            rng.uniform(0.2, 4),
        )

    history: list[Decision] = []
    for principal, kind, clear_rate in deciders:
        for w in range(weeks * 4):
            entity = rng.choice(ids)
            chosen = rng.choice(("fix", "hold", "acknowledge"))
            history.append(
                Decision(
                    entity_id=entity,
                    claim_kind=population.wrong_claim,
                    chosen=chosen,
                    decider=principal,
                    decider_kind=kind,
                    at=now - timedelta(days=rng.uniform(1, weeks * 7)),
                    cleared=rng.random() < clear_rate,
                )
            )

    items.sort(key=lambda i: (i.entity_id, i.claim_kind))
    history.sort(key=lambda d: d.at)
    return SimulatedFleet(site=site, items=tuple(items), history=tuple(history))


#: The robot fleet at scale. Forty endpoints on identical weights, which is
#: the point: the differences below are about where each one works, not
#: what it runs.
ROBOTS = Population(
    entity_kind="robot",
    prefix="rbt",
    size=40,
    unverified_claim="judgement",
    wrong_claim="task_outcome",
    degraded_claim="task_outcome",
    unverified_evidence=(
        "{id} has completed {n} picks since the model update. Every one was accepted "
        "on its own confidence; none has been checked against what was in the tote."
    ),
    wrong_evidence=(
        "{id} mislabelled {n} of its last 400 totes. Other endpoints on the same "
        "weights did not — the difference is the bay, not the software."
    ),
    degraded_evidence=(
        "Nothing has checked {id}'s work for {n} days, so whether it is still picking "
        "correctly is not known."
    ),
)

#: The contrasting fleet: the agent asserts about DATA rather than acting
#: on the world. Same generator, different vocabulary — which is the
#: abstraction check. A second generator here would mean the construct was
#: about robots after all.
INSTRUMENTS = Population(
    entity_kind="instrument",
    prefix="room",
    size=24,
    unverified_claim="reading_validity",
    wrong_claim="hold_point",
    degraded_claim="reading_link",
    unverified_evidence=(
        "The conform agent marked {n} of {id}'s readings valid today. They carry no "
        "unit, so 'valid' cannot mean within range."
    ),
    wrong_evidence=(
        "{n}% of {id}'s readings yesterday are exactly its probe's fault word, served "
        "downstream as a temperature."
    ),
    degraded_evidence=("Nothing has arrived from {id} for {n} hours. It reports every minute."),
)


def mixed(
    populations: tuple[tuple[Population, int], ...],
    *,
    site: str,
    now: datetime,
    seed: int = 7,
    deciders: tuple[tuple[str, str, float], ...] = (),
) -> SimulatedFleet:
    """Several unlike populations at ONE site, under one accountable person.

    The hardest abstraction test so far, and the one closest to a paying
    case. A warehouse fleet is homogeneous — forty of the same thing, and a
    construct can quietly encode "the thing" without anyone noticing. A
    farm is not: an autonomous tractor, an irrigation controller, a
    perimeter camera and a lighting circuit share nothing except an owner
    and a consequence for getting it wrong.

    If one brief can carry all four without special-casing any, the
    construct is about supervision. If it cannot, we learned it here rather
    than at a customer.

    What this exposes, stated because the fixture cannot fix it:
    **severity cannot come from claim status alone.** An irrigation valve
    stuck open and a lighting circuit left on are both `failed`, and they
    are not remotely the same problem — one floods a field and one wastes
    pennies. Today severity is the worst member status, which ranks them
    identically. The missing input is CONSEQUENCE, and it is a property of
    the endpoint kind rather than of the claim.
    """
    items: list[OversightItem] = []
    history: list[Decision] = []
    for offset, (population, _weight) in enumerate(populations):
        part = simulate(
            population,
            site=site,
            now=now,
            seed=seed + offset,
            deciders=deciders if offset == 0 else (),
        )
        items.extend(part.items)
        history.extend(part.history)
    items.sort(key=lambda i: (i.entity_kind, i.entity_id, i.claim_kind))
    history.sort(key=lambda d: d.at)
    return SimulatedFleet(site=site, items=tuple(items), history=tuple(history))


#: A farm's disparate autonomous equipment. Four unlike kinds, one owner.
TRACTORS = Population(
    entity_kind="tractor",
    prefix="trc",
    size=6,
    unverified_claim="field_work",
    wrong_claim="field_work",
    degraded_claim="position_fix",
    unverified_evidence=(
        "{id} worked {n} acres autonomously overnight. Nothing has inspected the "
        "result — the pass is recorded as done because it finished, not because it "
        "was checked."
    ),
    wrong_evidence=(
        "{id} left {n} skips in last night's pass. The rows it missed will not be "
        "caught until the crop shows it."
    ),
    degraded_evidence=(
        "{id} has been running on a degraded position fix for {n} hours, so how "
        "accurately it worked is not known."
    ),
)

IRRIGATION = Population(
    entity_kind="irrigation",
    prefix="zone",
    size=14,
    unverified_claim="water_applied",
    wrong_claim="valve_state",
    degraded_claim="soil_reading",
    unverified_evidence=(
        "The scheduler reports {id} received its full application. No flow meter "
        "reading backs that up, so the amount applied is the schedule's word."
    ),
    wrong_evidence=(
        "{id} has reported its valve open for {n} hours past its window. If that is "
        "true the field is being over-watered right now."
    ),
    degraded_evidence=(
        "No soil moisture reading from {id} for {n} days, so whether it needs water "
        "is being decided from the calendar rather than the ground."
    ),
)

PERIMETER = Population(
    entity_kind="camera",
    prefix="cam",
    size=9,
    unverified_claim="detection",
    wrong_claim="detection",
    degraded_claim="view",
    unverified_evidence=(
        "{id} raised {n} detections this week and none was reviewed, so what it is "
        "calling a person is not known."
    ),
    wrong_evidence=(
        "{id} raised {n} detections overnight that a review found were deer. Alarms "
        "that are usually wrong stop being answered."
    ),
    degraded_evidence=(
        "{id}'s view has been obstructed for {n} days — it is reporting nothing "
        "because it can see nothing."
    ),
)


def farm_at_scale(*, now: datetime | None = None, seed: int = 7) -> SimulatedFleet:
    """Disparate autonomous equipment under one operator."""
    return mixed(
        ((TRACTORS, 1), (IRRIGATION, 1), (PERIMETER, 1)),
        site="ridge-farm",
        now=now or datetime.now(UTC),
        seed=seed,
        deciders=(
            ("@dana:ridge-farm", "human", 0.79),
            ("@farm-agent:ridge-farm", "agent", 0.5),
        ),
    )


def robot_fleet_at_scale(*, now: datetime | None = None, seed: int = 7) -> SimulatedFleet:
    """The number-one scenario, with weeks of record behind it.

    The two deciders differ on purpose. The fleet agent decides more often
    and clears less — which is exactly the fact a calibration measurement
    should surface and nothing today can.
    """
    return simulate(
        ROBOTS,
        site="dc-3",
        now=now or datetime.now(UTC),
        seed=seed,
        deciders=(
            ("@sam:dc-3", "human", 0.82),
            ("@fleet-warden:dc-3", "agent", 0.54),
        ),
    )


def instrument_fleet_at_scale(*, now: datetime | None = None, seed: int = 7) -> SimulatedFleet:
    """The contrast, at the same scale and through the same generator."""
    return simulate(
        INSTRUMENTS,
        site="depot-west",
        now=now or datetime.now(UTC),
        seed=seed,
        deciders=(
            ("@robin:depot-west", "human", 0.77),
            ("@conform:depot-west", "agent", 0.61),
        ),
    )


def seed_record(session, fleet: SimulatedFleet, *, now: datetime) -> int:
    """Write the simulated history as REAL decision rows. Caller commits.

    The point of writing rows rather than handing `calibrate` the fixture:
    a measurement that reads the generator proves the generator. This goes
    through the same table the surface writes, so what is measured is the
    record.

    The mapping carries the censoring honestly, which is the part that
    matters. A decision whose situation cleared gets ``outcome =
    resolved``. One that did not gets NO outcome — because that is exactly
    what the record can say about it, and the whole reason `calibrate`
    reports a bound. Writing "did not clear" would invent a fact the
    platform has no way to observe and would make the measurement look
    sharper than the world allows.
    """
    from axiom.extensions.builtins.receipts.cases import case_id_for
    from axiom.extensions.builtins.receipts.db_models import CaseVerdict
    from axiom.extensions.builtins.receipts.verdicts import OUTCOME_RESOLVED

    written = 0
    for d in fleet.history:
        entity_kind = next(
            (i.entity_kind for i in fleet.items if i.entity_id == d.entity_id), "robot"
        )
        # The status the condition was in when it was decided. The history
        # records decisions about trouble, and the trouble kind is what
        # `calibrate` groups by.
        condition = f"{d.claim_kind}|failed"
        row = CaseVerdict(
            site=fleet.site,
            case_id=case_id_for(fleet.site, entity_kind, d.entity_id),
            condition=condition,
            decision_type="choice",
            options=["fix", "hold", "acknowledge"],
            chosen=d.chosen,
            decider=d.decider,
            decider_kind=d.decider_kind,
            evidence=f"{d.entity_id}: {d.claim_kind} was in trouble",
            decided_at=d.at,
        )
        if d.cleared:
            row.outcome = OUTCOME_RESOLVED
            row.outcome_at = d.at + timedelta(hours=6)
        session.add(row)
        written += 1
    return written


__all__ = [
    "DEGRADED",
    "HEALTHY",
    "INSTRUMENTS",
    "ROBOTS",
    "UNVERIFIED",
    "WRONG",
    "Decision",
    "Population",
    "SimulatedFleet",
    "farm_at_scale",
    "instrument_fleet_at_scale",
    "mixed",
    "robot_fleet_at_scale",
    "seed_record",
    "simulate",
]
