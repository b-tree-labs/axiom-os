# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``attest.obligations`` (read) and ``attest.obligations_tick`` (scheduled)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from axiom.infra.skills import SkillResult

from .. import obligations, registry, store
from ..logbooks import LogbookError
from ._common import fail, resolve_site


def _iso(st: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in st.items()}


def status(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    site, err = resolve_site(params)
    if err:
        return fail(err)
    try:
        logbooks = (
            [registry.get(params["logbook"])] if params.get("logbook") else registry.all_logbooks()
        )
    except LogbookError as exc:
        return fail(str(exc))
    now = datetime.now(UTC)
    states = [
        _iso(st)
        for b in logbooks
        if b.obligations
        for st in obligations.evaluate(site, b.id, now=now)
    ]
    return SkillResult(ok=True, value={"site_id": site, "obligations": states})


def tick(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Run by the schedule every minute: record, publish and notify changes
    for every open interval with an obligation."""
    with store.session_scope() as s:
        open_ = s.execute(
            text("SELECT DISTINCT site_id, logbook FROM attest_intervals WHERE closed_at IS NULL")
        ).all()
    now = datetime.now(UTC)
    changes: list[dict[str, Any]] = []
    for row in open_:
        try:
            if not registry.get(row.logbook).obligations:
                continue
        except LogbookError:
            continue
        changes += [
            _iso(c)
            for c in obligations.tick(row.site_id, row.logbook, now=now, state_dir=ctx.state_dir)
        ]
    return SkillResult(
        ok=True,
        value={"changes": changes},
        actions_taken=[f"{c['state']}: {c['type']} at {c['site_id']}" for c in changes],
    )
