# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Report ingestion (ADR-119 D2/D3, spec-fleet-console §3).

Pure persistence logic: the API route resolves the credential and hands
``site`` in — site attribution NEVER comes from the payload (the
ingest-sink tenancy rule). Refusals are atomic: one bad report refuses
the whole batch, so a pusher never has to guess which half landed.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from axiom.extensions.builtins.fleet.db_models import (
    FleetLatest,
    FleetNode,
    FleetReport,
)
from axiom.extensions.builtins.fleet.status import REPORT_KINDS


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


DEFAULT_MAX_REPORTS = _env_int("AXIOM_FLEET_MAX_REPORTS", 50)
DEFAULT_MAX_PAYLOAD_CHARS = _env_int("AXIOM_FLEET_MAX_PAYLOAD_CHARS", 200_000)


class IngestRefused(ValueError):
    """The whole batch was refused; nothing was written."""


def _as_utc(dt: datetime) -> datetime:
    """SQLite round-trips DateTime columns naive; treat naive as UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


@dataclass(frozen=True)
class IngestOutcome:
    accepted: int
    report_ids: tuple[str, ...] = field(default_factory=tuple)


def ingest_reports(
    session: Any,
    *,
    site: str,
    node_id: str,
    reporter_principal: str,
    reports: Sequence[dict],
    now: datetime | None = None,
    cadences: dict[str, int] | None = None,
    envelope_hash: str | None = None,
    max_reports: int = DEFAULT_MAX_REPORTS,
    max_payload_chars: int = DEFAULT_MAX_PAYLOAD_CHARS,
) -> IngestOutcome:
    """Validate and persist a batch of reports for one node.

    Caller commits (store convention). Raises :class:`IngestRefused`
    before any write on: unknown kind, cap breach, or a node_id already
    owned by a different site.
    """
    now = now or datetime.now(UTC)

    if len(reports) > max_reports:
        raise IngestRefused(f"{len(reports)} reports exceeds cap {max_reports}")
    for r in reports:
        kind = r.get("kind")
        if kind not in REPORT_KINDS:
            raise IngestRefused(f"unknown report kind {kind!r}; known: {REPORT_KINDS}")
        payload = r.get("payload")
        if not isinstance(payload, dict):
            raise IngestRefused(f"report kind {kind!r} payload must be an object")
        if len(json.dumps(payload)) > max_payload_chars:
            raise IngestRefused(f"payload for kind {kind!r} exceeds {max_payload_chars} chars")

    node = session.get(FleetNode, node_id)
    if node is None:
        node = FleetNode(node_id=node_id, site=site, enrolled_at=now, cadences=cadences)
        session.add(node)
    elif node.site != site:
        # A leaked node name must not let one tenant overwrite another's row.
        raise IngestRefused(f"node {node_id!r} is enrolled under a different site")
    elif cadences:
        node.cadences = cadences

    report_ids = []
    for r in reports:
        report = FleetReport(
            id=uuid.uuid4().hex,
            node_id=node_id,
            site=site,
            kind=r["kind"],
            payload=r["payload"],
            received_at=now,
            reporter_principal=reporter_principal,
            envelope_hash=envelope_hash,
            signature_state="unverified",
        )
        session.add(report)
        report_ids.append(report.id)

        latest = session.get(FleetLatest, (node_id, r["kind"]))
        if latest is None:
            session.add(
                FleetLatest(
                    node_id=node_id,
                    kind=r["kind"],
                    report_id=report.id,
                    received_at=now,
                )
            )
        elif _as_utc(latest.received_at) <= now:
            latest.report_id = report.id
            latest.received_at = now

    return IngestOutcome(accepted=len(report_ids), report_ids=tuple(report_ids))
