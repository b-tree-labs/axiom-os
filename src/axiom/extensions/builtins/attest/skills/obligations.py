# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``attest.obligations`` (read) and ``attest.obligations_tick`` (scheduled; it also evaluates the
logs' predicates and catches up their cross-checks, #1816)."""

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
    if not params.get("logbook"):
        # Over every log: only those the caller may read, each by the one check (unit LD-1).
        from .. import doors

        a = doors.current()
        caller = a.caller if a is not None else doors.caller_from_ctx(ctx)
        logbooks = [b for b in logbooks if doors.readable(caller, "attest.obligations", site, b.id)]
    now = datetime.now(UTC)
    states = [
        _iso(st)
        for b in logbooks
        if b.obligations
        for st in obligations.evaluate(site, b.id, now=now)
    ]
    misses = [
        {"logbook": b.id, **m} for b in logbooks if b.obligations for m in obligations.misses(site, b.id)
    ]
    return SkillResult(ok=True, value={"site_id": site, "obligations": states, "misses": misses})


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
    # Predicates and the cross-check catch-up ride the same tick (#1816).
    from .. import predicates

    facts = [_iso(c) for c in predicates.tick_all(now=now, state_dir=ctx.state_dir)]
    return SkillResult(
        ok=True,
        value={"changes": changes, "predicates": facts},
        actions_taken=[f"{c['state']}: {c['type']} at {c['site_id']}" for c in changes]
        + [f"{c['state']}: {c['predicate']} at {c['site_id']}" for c in facts],
    )
