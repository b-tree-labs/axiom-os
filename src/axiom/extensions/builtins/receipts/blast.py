# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What else this case touches — computed, never guessed (PRD R19).

"Unknown" is the answer that makes a person do the reachability in
their head, so the surface never says it. Either an edge is DECLARED
somewhere the platform can read, and we count it and name where it came
from, or nothing declares one and we say **zero declared dependents** —
which is a fact about the declarations, not about the world, and the
line says so.

Every edge here is read from something a node or an operator actually
declared:

- **services** — the names inside the node's own ``service_health``
  report. The node declares what it runs; if the node is troubled,
  those are what a person is being asked about.
- **schedules** — the singleton scheduler lease: when THIS node holds
  it, every registered schedule fires here, so its trouble is their
  trouble. (Leaseless deployments declare nothing and get zero.)
- **people** — the principal that reports for this node (accountable
  for its reporting) and anyone who has decided about this case
  before: they asked to be told once already.

Sources are returned alongside the counts so the surface can show
*where the number came from* — a number whose provenance is invisible
is the same problem as "Unknown" wearing a different hat.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from axiom.extensions.builtins.receipts.cases import Case


@dataclass(frozen=True)
class Blast:
    """Reach, with its provenance. ``declared`` False = nothing declared
    an edge (not "we could not compute it")."""

    systems: list[str] = field(default_factory=list)
    schedules: list[str] = field(default_factory=list)
    people: list[str] = field(default_factory=list)
    #: human-readable provenance, one per contributing source
    sources: list[str] = field(default_factory=list)

    @property
    def declared(self) -> bool:
        return bool(self.systems or self.schedules or self.people)

    def payload(self) -> dict:
        return {
            "systems": list(self.systems),
            "schedules": list(self.schedules),
            "people": list(self.people),
            "sources": list(self.sources),
            "declared": self.declared,
            "summary": self.summary(),
        }

    def summary(self) -> str:
        if not self.declared:
            return (
                "0 declared dependents — nothing on this node declares what "
                "depends on it, so there is nothing to compute from"
            )
        parts = []
        for one, many, items in (
            ("system", "systems", self.systems),
            ("schedule", "schedules", self.schedules),
            ("person", "people", self.people),
        ):
            if items:
                parts.append(f"{len(items)} {one if len(items) == 1 else many}")
        return " · ".join(parts)


def _services_of(fleet_session, node_id: str) -> list[str]:
    """The service names the node itself declared in its latest health
    report. Read-only; a node that reports no services declares none."""
    from axiom.extensions.builtins.fleet.db_models import FleetLatest, FleetReport

    row = (
        fleet_session.query(FleetReport)
        .join(FleetLatest, FleetLatest.report_id == FleetReport.id)
        .filter(FleetLatest.node_id == node_id, FleetLatest.kind == "service_health")
        .first()
    )
    payload = getattr(row, "payload", None) or {}
    services = payload.get("services") or []
    return sorted(str(s.get("name")) for s in services if isinstance(s, dict) and s.get("name"))


def _reporter_of(fleet_session, node_id: str) -> str | None:
    """Who reports for this node — accountable for its reporting."""
    from axiom.extensions.builtins.fleet.db_models import FleetReport

    row = (
        fleet_session.query(FleetReport)
        .filter(FleetReport.node_id == node_id)
        .order_by(FleetReport.received_at.desc())
        .first()
    )
    return getattr(row, "reporter_principal", None)


def _schedules_if_leader(node_id: str) -> tuple[list[str], bool]:
    """Registered schedules, when THIS node holds the scheduler lease.

    Returns (names, reachable). ``reachable`` False means the schedule
    extension is not installed or its store is unavailable — which the
    caller reports as a missing source rather than as zero.
    """
    try:
        from axiom.extensions.builtins.schedule import store as schedule_store
        from axiom.extensions.builtins.schedule.db_models import (
            ScheduleDefinition,
            ScheduleLease,
        )
    except Exception:  # noqa: BLE001 — extension absent is not an error
        return [], False
    try:
        with schedule_store.session_scope() as s:
            lease = s.query(ScheduleLease).first()
            if lease is None or lease.node_id != node_id:
                return [], True  # reachable, and this node runs nothing
            return sorted(d.name for d in s.query(ScheduleDefinition).all()), True
    except Exception:  # noqa: BLE001 — unavailable store: say so, don't guess
        return [], False


def compute_blast(
    fleet_session,
    case: Case,
    *,
    prior_deciders: list[str] | None = None,
) -> Blast:
    """Reach for one case, from declared edges only."""
    if case.entity_kind != "node":
        # Other entity kinds declare their edges elsewhere; until those
        # sources are wired, say so rather than imply zero.
        return Blast(sources=[f"no edge source wired for {case.entity_kind} entities"])

    systems = _services_of(fleet_session, case.entity_id)
    sources: list[str] = []
    if systems:
        sources.append(f"{len(systems)} service(s) this node reports")

    schedules, reachable = _schedules_if_leader(case.entity_id)
    if schedules:
        sources.append(f"{len(schedules)} schedule(s) — this node holds the lease")
    elif not reachable:
        sources.append("schedules: not readable here (extension absent or store down)")

    people: list[str] = []
    reporter = _reporter_of(fleet_session, case.entity_id)
    if reporter:
        people.append(reporter)
        sources.append("the principal reporting for this node")
    for decider in prior_deciders or []:
        if decider not in people:
            people.append(decider)
    if prior_deciders:
        sources.append(f"{len(prior_deciders)} prior decider(s) on this case")

    return Blast(systems=systems, schedules=schedules, people=people, sources=sources)


__all__ = ["Blast", "compute_blast"]
