# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.ingest`` — gated source → bronze → RAG pass for one connector.

Verb-grammar note: ``ingest`` is the verb; the connector is a flag
parameter (``--connector``), not a kebab-compound suffix. This skill
wraps the existing ``run_ingest`` implementation, which already
applies ``guarded_act`` (ADR-045 D6) per-item.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from axiom.governance.classification import Classification
from axiom.infra.skills import SkillContext, SkillResult

from .. import _authz
from ..agents.plinth.skills.run_tabular_ingest import run_connector_ingest
from . import verify

log = logging.getLogger(__name__)


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    connector = params.get("connector")
    if not connector:
        return SkillResult(ok=False, errors=["missing required param: connector"])

    since_raw = params.get("since")
    since = datetime.fromisoformat(since_raw) if since_raw else None
    volume_mode = params.get("volume_mode", "confirm")
    actor = params.get("actor")
    workers = params.get("workers")
    workers = int(workers) if workers else None

    actions: list[str] = []

    # Loud diagnose: a missing OCR/PDF/office-doc lib silently degrades the
    # extract lane to text-only or fails quietly per item. Warn at the top of
    # the run (not buried in a per-item traceback) with an actionable fix.
    missing = verify.missing_extraction_deps()
    if missing:
        warning = (
            f"{verify.EXTRACTION_REMEDIATION} "
            f"(not importable: {missing}) — non-text documents may extract as "
            "text-only or be skipped this run"
        )
        log.warning(warning)
        actions.append(f"WARNING: {warning}")
    with _authz.action(
        verb="ingest",
        resource=f"data-platform://connector/{connector}",
        classification=Classification.INTERNAL,
        actor=actor,
    ) as act:
        actions.append(f"audit-receipt: {act.receipt_id}")
        # Dispatch by the connector's provider shape: document → run_ingest
        # (bronze → RAG), tabular → run_tabular_ingest (rows → typed bronze).
        report = run_connector_ingest(
            connector,
            since=since,
            state_dir=ctx.state_dir,
            volume_mode=volume_mode,
            max_workers=workers,
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
            # The generic per-stage funnel (in/out/dropped/failed by stage) —
            # the single source of truth for what happened in this run.
            "funnel": report.funnel,
        },
        actions_taken=actions,
        errors=errors,
    )
