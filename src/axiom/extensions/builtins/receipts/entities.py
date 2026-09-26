# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What the platform knows about a thing it has named.

Founder direction (2026-09-25): "each identified item/entity needs a way
to drill down to learn more."

Every name on a case — the node, the person who decided, the service in
its reach — is a thing the reader may want to know more about, and until
now naming it was the end of the road. This module answers the one
question a drill-down asks: what do we actually know?

Two rules make it safe to build a profile out of:

**Only what is recorded.** Every fact below is read from something the
platform stored: a node's own reports, the append-only decision record,
the service list inside a health report. Nothing is inferred, and a
profile with nothing in it is not rendered at all — an entity kind we
have no facts for answers "no profile", and the surface then shows no
affordance rather than a page that apologises.

**Facts carry their source.** A profile is a claim about somebody's
node or somebody's decisions; the reader gets to see where each line
came from, the same rule reach already follows.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

#: Entity kinds this module can say anything about. A kind absent here is
#: one nobody has recorded facts for yet, and the surface must not offer
#: a drill-down into nothing.
KNOWN_KINDS = ("node", "person", "service")


@dataclass(frozen=True)
class Fact:
    """One line of a profile. ``note`` qualifies it where a bare value
    would overstate what is known."""

    label: str
    value: str
    note: str = ""

    def payload(self) -> dict:
        return {"label": self.label, "value": self.value, "note": self.note}


@dataclass(frozen=True)
class EntityProfile:
    kind: str
    entity_id: str
    title: str
    subtitle: str
    facts: list[Fact] = field(default_factory=list)
    #: where the facts came from, in the reader's words
    sources: list[str] = field(default_factory=list)

    @property
    def known(self) -> bool:
        return bool(self.facts)

    def payload(self) -> dict:
        return {
            "kind": self.kind,
            "entity_id": self.entity_id,
            "title": self.title,
            "subtitle": self.subtitle,
            "facts": [f.payload() for f in self.facts],
            "sources": list(self.sources),
        }


def _node_profile(fleet_session, node_id: str, *, now=None) -> EntityProfile:
    """A node, from its own reports. It declares what it runs and when it
    last said so; that is the whole profile, and it is enough."""
    from datetime import UTC, datetime

    from axiom.extensions.builtins.fleet.db_models import FleetLatest, FleetReport
    from axiom.extensions.builtins.receipts.conditions import words
    from axiom.infra.time_utils import time_ago

    rows = (
        fleet_session.query(FleetReport)
        .join(FleetLatest, FleetLatest.report_id == FleetReport.id)
        .filter(FleetLatest.node_id == node_id)
        .all()
    )
    if not rows:
        return EntityProfile(kind="node", entity_id=node_id, title=node_id, subtitle="node")

    when = now or datetime.now(UTC)
    newest = max(rows, key=lambda r: r.received_at)
    kinds = sorted({words(r.kind) for r in rows})
    facts = [
        Fact(label="Site", value=str(getattr(newest, "site", "") or "not recorded")),
        Fact(label="Reports", value=", ".join(kinds)),
        Fact(
            label="Last heard from",
            value=time_ago(newest.received_at, now=when, compact=False),
        ),
    ]
    reporter = getattr(newest, "reporter_principal", "") or ""
    if reporter:
        facts.append(Fact(label="Reported by", value=reporter))

    services = []
    for row in rows:
        if row.kind == "service_health":
            payload = row.payload or {}
            services = [
                str(s.get("name")) for s in payload.get("services", []) if isinstance(s, dict)
            ]
    if services:
        facts.append(Fact(label="Runs", value=", ".join(sorted(filter(None, services)))))

    return EntityProfile(
        kind="node",
        entity_id=node_id,
        title=node_id,
        subtitle="node",
        facts=facts,
        sources=["what this node reported about itself"],
    )


def _person_profile(receipts_session, handle: str, *, now=None) -> EntityProfile:
    """A person, from the decision record.

    This is the profile that is actually worth having: not who they are,
    which the platform does not know, but what they have decided and
    whether it worked. It is read from append-only rows, so it cannot
    flatter anybody.
    """
    from datetime import UTC, datetime

    from axiom.extensions.builtins.receipts.db_models import CaseVerdict
    from axiom.extensions.builtins.receipts.verdicts import decision_taken_label
    from axiom.infra.time_utils import time_ago

    rows = (
        receipts_session.query(CaseVerdict)
        .filter(CaseVerdict.decider == handle)
        .order_by(CaseVerdict.decided_at.desc())
        .all()
    )
    if not rows:
        # Also accept the display name, since that is what a reader clicks.
        rows = (
            receipts_session.query(CaseVerdict)
            .filter(CaseVerdict.decider_label == handle)
            .order_by(CaseVerdict.decided_at.desc())
            .all()
        )
    if not rows:
        return EntityProfile(kind="person", entity_id=handle, title=handle, subtitle="person")

    when = now or datetime.now(UTC)
    label = next((r.decider_label for r in rows if r.decider_label), "") or handle
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.chosen] = counts.get(r.chosen, 0) + 1
    chose = ", ".join(
        f"{n} {decision_taken_label(choice).lower()}" for choice, n in sorted(counts.items())
    )

    facts = [
        Fact(label="Decisions", value=str(len(rows))),
        Fact(label="Chose", value=chose),
        Fact(
            label="Last decided",
            value=time_ago(rows[0].decided_at, now=when, compact=False),
        ),
    ]
    cleared = [
        (r.outcome_at - r.decided_at).total_seconds() / 60.0
        for r in rows
        if r.outcome_at is not None and r.decided_at is not None
    ]
    if cleared:
        facts.append(
            Fact(
                label="Cleared after",
                value=f"{round(statistics.median(cleared), 1)} min",
                note=f"median of {len(cleared)} decided and observed",
            )
        )
    else:
        # Absence stated, not left blank: no outcome has been observed for
        # anything they decided, which is a fact about the record.
        facts.append(
            Fact(label="Cleared after", value="not observed yet", note="no outcome has arrived")
        )

    return EntityProfile(
        kind="person",
        entity_id=handle,
        title=label,
        subtitle="person" if label == handle else f"person · {handle}",
        facts=facts,
        sources=["the decision record, which is append-only"],
    )


def _service_profile(fleet_session, name: str, *, now=None) -> EntityProfile:
    """A service, from the health report of whichever node runs it."""
    from datetime import UTC, datetime

    from axiom.extensions.builtins.fleet.db_models import FleetLatest, FleetReport
    from axiom.infra.time_utils import time_ago

    rows = (
        fleet_session.query(FleetReport)
        .join(FleetLatest, FleetLatest.report_id == FleetReport.id)
        .filter(FleetLatest.kind == "service_health")
        .all()
    )
    when = now or datetime.now(UTC)
    for row in rows:
        for entry in (row.payload or {}).get("services", []):
            if not isinstance(entry, dict) or entry.get("name") != name:
                continue
            facts = [
                Fact(label="Runs on", value=str(row.node_id)),
                Fact(label="Reported", value=str(entry.get("status") or "not stated")),
            ]
            latency = entry.get("latency_ms")
            if latency is not None:
                facts.append(Fact(label="Responded in", value=f"{latency} ms"))
            else:
                facts.append(
                    Fact(
                        label="Responded in",
                        value="not measured",
                        note="a health claim without a time is unproven",
                    )
                )
            facts.append(
                Fact(
                    label="Last heard from",
                    value=time_ago(row.received_at, now=when, compact=False),
                )
            )
            return EntityProfile(
                kind="service",
                entity_id=name,
                title=name,
                subtitle=f"service on {row.node_id}",
                facts=facts,
                sources=[f"the health report {row.node_id} pushed"],
            )
    return EntityProfile(kind="service", entity_id=name, title=name, subtitle="service")


def profile_for(
    kind: str,
    entity_id: str,
    *,
    fleet_session=None,
    receipts_session=None,
    now=None,
) -> EntityProfile:
    """What we know about one named thing. Never invents; a kind with no
    recorded facts comes back ``known=False`` and the caller 404s."""
    if kind == "node" and fleet_session is not None:
        return _node_profile(fleet_session, entity_id, now=now)
    if kind == "person" and receipts_session is not None:
        return _person_profile(receipts_session, entity_id, now=now)
    if kind == "service" and fleet_session is not None:
        return _service_profile(fleet_session, entity_id, now=now)
    return EntityProfile(kind=kind, entity_id=entity_id, title=entity_id, subtitle=kind)


__all__ = ["KNOWN_KINDS", "EntityProfile", "Fact", "profile_for"]
