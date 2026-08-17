# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.refresh`` — CDC / incremental delta ingest for one connector.

Derives a per-connector *watermark* off ``max(chunks.indexed_at)`` (scoped to
the connector, preferring the canonical ``data_source`` column and falling
back to the ``source_path`` top-folder — same attribution model as
``data.activity``), backs it off by ``overlap_minutes`` for safety, then hands
that timestamp to the existing :func:`run_ingest` as its ``since=`` cursor.
The source's own ``list_changed(since=...)`` does the true delta walk (Box,
e.g., has no server-side since filter so it walks + filters by ``modified_at``).

No prior watermark (a fresh corpus) → full pass (``since=None``). This is a
thin orchestration over ``run_ingest``; it reinvents no ingestion.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

from axiom.governance.classification import Classification
from axiom.infra.skills import SkillContext, SkillResult

from .. import _authz
from ..agents.plinth.skills.run_ingest import run_ingest

# Shared folder↔connector map (single source of truth in skills/activity.py,
# loaded from the site-supplied ``data_platform.connector_labels`` config) —
# used to scope the watermark query when attribution is by source_path
# top-folder (no data_source column).
from .activity import connector_by_folder  # noqa: E402


def _watermark(cur, connector: str) -> datetime | None:
    """Return ``max(indexed_at)`` for ``connector`` or None if it has none.

    Prefers the canonical ``data_source`` column; falls back to matching the
    ``source_path`` top-folder (robust to schema drift). The folder is the
    connector's registered Box folder when known, else the connector name.
    """
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name='chunks' AND column_name='data_source'"
    )
    has_ds = cur.fetchone() is not None
    if has_ds:
        cur.execute(
            "SELECT max(indexed_at) FROM chunks WHERE data_source = %s",
            (connector,),
        )
    else:
        folder_by_connector = {v: k for k, v in connector_by_folder().items()}
        folder = folder_by_connector.get(connector, connector)
        cur.execute(
            "SELECT max(indexed_at) FROM chunks "
            "WHERE source_path LIKE '/' || %s || '%%'",
            (folder,),
        )
    row = cur.fetchone()
    return row[0] if row else None


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    connector = params.get("connector")
    if not connector:
        return SkillResult(ok=False, errors=["missing required param: connector"])

    try:
        overlap_minutes = int(params.get("overlap_minutes", 60))
    except (TypeError, ValueError):
        return SkillResult(ok=False, errors=["overlap_minutes must be an integer"])
    if overlap_minutes < 0:
        return SkillResult(ok=False, errors=["overlap_minutes must be >= 0"])

    dsn = os.environ.get("DP1_RAG_DSN") or os.environ.get("DATABASE_URL")
    if not dsn:
        return SkillResult(ok=False, errors=["DP1_RAG_DSN / DATABASE_URL unset"])

    try:
        import psycopg2
    except ImportError:
        return SkillResult(ok=False, errors=["psycopg2 not installed"])

    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            watermark = _watermark(cur, connector)
    finally:
        conn.close()

    # Back the watermark off by the overlap so items written in the seconds
    # around the last run aren't missed. None → full pass.
    since = watermark - timedelta(minutes=overlap_minutes) if watermark else None
    full_pass = since is None

    actions: list[str] = []
    with _authz.action(
        verb="refresh",
        resource=f"data-platform://connector/{connector}",
        classification=Classification.INTERNAL,
        actor=params.get("actor"),
    ) as act:
        actions.append(f"audit-receipt: {act.receipt_id}")
        actions.append(
            f"watermark: {'(none — full pass)' if full_pass else since.isoformat()} "
            f"(max indexed_at {watermark.isoformat() if watermark else 'none'} "
            f"- {overlap_minutes}m overlap)"
        )
        report = run_ingest(
            connector,
            since=since,
            state_dir=ctx.state_dir,
            volume_mode="off",
            max_workers=8,
        )

    if report.proceed:
        actions.append(
            f"refresh pass: seen={report.items_seen} "
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
            "full_pass": full_pass,
            "watermark": watermark.isoformat() if watermark else None,
            "since": since.isoformat() if since else None,
            "overlap_minutes": overlap_minutes,
            "proceed": report.proceed,
            "items_seen": report.items_seen,
            "items_landed": report.items_landed,
            "items_failed": report.items_failed,
            "refused_reason": report.refused_reason,
        },
        actions_taken=actions,
        errors=errors,
    )
