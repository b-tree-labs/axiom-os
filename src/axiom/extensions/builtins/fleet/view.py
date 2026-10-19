# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The fleet status view (spec-fleet-console §4-§5): read-time assembly of
per-node, per-kind evaluations with worst-wins rollup. No stored status —
judgment happens at read time so it cannot itself go stale."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from axiom.extensions.builtins.fleet.db_models import (
    FleetLatest,
    FleetNode,
    FleetReport,
)
from axiom.extensions.builtins.fleet.status import Status, evaluate_report, rollup

# Fallbacks when a node declared no cadence for a kind (spec §5).
DEFAULT_CADENCES = {
    # A roster changes when an operator changes it, not on a clock, so this is
    # a staleness bound on the PUSH rather than a promise about the agents.
    "agents": 900,
    "heartbeat": 900,
    "service_health": 900,
    "backup": 86_400,
    "backup_validate": 86_400,
    "canary": 86_400,
    "versions": 86_400,
}


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def fleet_status(
    session,
    *,
    sites: list[str] | None = None,
    now: datetime | None = None,
    include_archived: bool = False,
) -> dict:
    """Assemble the console view. ``sites=None`` means the caller's scope
    is unbounded — API callers pass the resolved site scope, never raw
    user input."""
    now = now or datetime.now(UTC)

    node_q = select(FleetNode)
    if sites is not None:
        node_q = node_q.where(FleetNode.site.in_(sites))
    if not include_archived:
        node_q = node_q.where(FleetNode.archived_at.is_(None))
    nodes = session.scalars(node_q.order_by(FleetNode.site, FleetNode.node_id)).all()

    out_nodes = []
    for node in nodes:
        cadences = {**DEFAULT_CADENCES, **(node.cadences or {})}
        latest_rows = session.scalars(
            select(FleetLatest).where(FleetLatest.node_id == node.node_id)
        ).all()

        kinds: dict[str, dict] = {}
        statuses: list[Status] = []
        for latest in latest_rows:
            report = session.get(FleetReport, latest.report_id)
            evaluation = evaluate_report(
                kind=latest.kind,
                payload=report.payload,
                received_at=_as_utc(latest.received_at),
                cadence_seconds=cadences[latest.kind],
                now=now,
                node_id=node.node_id,
            )
            statuses.append(evaluation.status)
            kinds[latest.kind] = {
                "status": evaluation.status.value,
                "evidence": evaluation.evidence,
                "received_at": _as_utc(latest.received_at).isoformat(),
                "signature_state": report.signature_state,
                # The declared cadence backs the surface's freshness bar
                # (PRD R2). Exposed so no client ever hardcodes cadences —
                # staleness math stays server-declared (R3).
                "cadence_seconds": cadences[latest.kind],
                # How the judgement was reached, so a reader can redo the
                # arithmetic instead of believing it.
                "derivation": evaluation.derivation,
            }

        out_nodes.append(
            {
                "node_id": node.node_id,
                "site": node.site,
                "display_name": node.display_name,
                "profile": node.profile,
                "rollup": rollup(statuses).value,
                "kinds": kinds,
            }
        )

    return {"generated_at": now.isoformat(), "nodes": out_nodes}


def agent_environments(
    session,
    *,
    sites: list[str] | None = None,
    include_archived: bool = False,
) -> list[dict]:
    """Every environment whose agents this caller is entitled to see.

    An ENVIRONMENT is one node that has pushed an agent roster: a laptop, a
    site node, a cloud collector. It carries the name its operator
    gave it (``display_name``) and the site that decides who may read it.

    ``sites=None`` means the caller's scope is unbounded. Otherwise the list
    is the RESOLVED scope — derived from the credential, never from anything
    the client sent — and a node outside it is not filtered out of the answer
    so much as never selected into it. That distinction matters: there is no
    code path here where an out-of-scope environment exists in a result and
    is then removed, so there is none where removing it can be forgotten.

    Each environment reports its own coverage. Those figures are NOT summed
    across environments and there is no total here, because a capability
    registry is per-install: adding two nodes' "62 of 120" together would
    produce a number that describes nothing.
    """
    node_q = select(FleetNode)
    if sites is not None:
        node_q = node_q.where(FleetNode.site.in_(sites))
    if not include_archived:
        node_q = node_q.where(FleetNode.archived_at.is_(None))
    nodes = session.scalars(node_q.order_by(FleetNode.site, FleetNode.node_id)).all()

    out: list[dict] = []
    for node in nodes:
        latest = session.scalar(
            select(FleetLatest).where(
                FleetLatest.node_id == node.node_id, FleetLatest.kind == "agents"
            )
        )
        if latest is None:
            # A node that has never pushed a roster is not an environment with
            # no agents; it is an environment that has not said. Omitted here
            # rather than rendered as empty, which would read as "nothing is
            # running there".
            continue
        report = session.get(FleetReport, latest.report_id)
        payload = (report.payload if report else None) or {}
        agents = payload.get("agents")
        env = {
            "id": node.node_id,
            "name": node.display_name or node.node_id,
            "site": node.site,
            "local": False,
            "agents": agents if isinstance(agents, list) else [],
            "reportedAt": _as_utc(latest.received_at).isoformat(),
            "collectedAt": payload.get("collectedAt"),
        }
        coverage = payload.get("coverage")
        if isinstance(coverage, dict):
            env["coverage"] = coverage
        out.append(env)
    return out
