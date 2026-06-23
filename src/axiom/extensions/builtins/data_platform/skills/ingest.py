# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.ingest`` — gated Box → bronze → RAG pass for one connector.

Verb-grammar note: ``ingest`` is the verb; the connector is a flag
parameter (``--connector``), not a kebab-compound suffix. This skill
wraps the existing ``run_ingest`` implementation, which already
applies ``guarded_act`` (ADR-045 D6) per-item.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from axiom.governance.classification import Classification
from axiom.infra.skills import SkillContext, SkillResult

from .. import _authz
from ..agents.plinth.skills.run_ingest import run_ingest


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    connector = params.get("connector")
    if not connector:
        return SkillResult(ok=False, errors=["missing required param: connector"])

    since_raw = params.get("since")
    since = datetime.fromisoformat(since_raw) if since_raw else None
    volume_mode = params.get("volume_mode", "confirm")
    actor = params.get("actor")

    actions: list[str] = []
    with _authz.action(
        verb="ingest",
        resource=f"data-platform://connector/{connector}",
        classification=Classification.INTERNAL,
        actor=actor,
    ) as act:
        actions.append(f"audit-receipt: {act.receipt_id}")
        report = run_ingest(
            connector,
            since=since,
            state_dir=ctx.state_dir,
            volume_mode=volume_mode,
        )

    if report.proceed:
        actions.append(
            f"ingest pass: seen={report.items_seen} "
            f"landed={report.items_landed} failed={report.items_failed}"
        )
    else:
        actions.append(f"REFUSED: {report.refused_reason}")

    ok = report.proceed and report.items_failed == 0
    errors: list[str] = []
    if not report.proceed:
        errors.append(report.refused_reason)

    return SkillResult(
        ok=ok,
        value={
            "connector": report.connector,
            "proceed": report.proceed,
            "items_seen": report.items_seen,
            "items_landed": report.items_landed,
            "items_failed": report.items_failed,
            "refused_reason": report.refused_reason,
        },
        actions_taken=actions,
        errors=errors,
    )
