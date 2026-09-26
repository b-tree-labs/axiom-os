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
