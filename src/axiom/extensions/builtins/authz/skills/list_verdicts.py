# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``audit.list`` — list authorization verdicts (PRD §5.4).

CLI shape::

    axi audit list [--since 7d] [--primitive notification] [--actor @jim:example-org]
                   [--decision permit|deny|propose_to_human|...] [--limit 50]
                   [--json]

Filters are AND-composed. ``--primitive`` matches the leading
dotted-segment of ``intent`` (e.g. ``notification`` matches
``notification.send.email``). Most recent first.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from sqlalchemy.orm import Session

from axiom.infra.skills import SkillContext, SkillResult

from ..db_models import Verdict
from ._since import parse_since

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 500


@contextmanager
def _default_session() -> Iterator[Session]:
    """Open a session against the ``authz`` schema. Indirection lets
    tests inject an in-memory session via ``params['_session_cm']``."""
    from axiom.infra.db import session_for  # local import — keeps the
    # skill importable in environments without a DB driver installed.
    with session_for("authz") as s:
        yield s


def _resolve_session(params: dict[str, Any]):
    """Use the test-injected session if present, else the real one."""
    cm = params.get("_session_cm")
    return cm() if cm is not None else _default_session()


def _to_row(v: Verdict) -> dict[str, Any]:
    return {
        "id": v.id,
        "decided_at": v.decided_at.isoformat() if v.decided_at else None,
        "actor": v.actor,
        "intent": v.intent,
        "resource": v.resource,
        "classification": v.classification,
        "decision": v.decision,
        "reason": v.reason,
        "federation_origin": v.federation_origin,
    }


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    # --- params parsing ---------------------------------------------------
    try:
        since_dt = parse_since(params["since"]) if params.get("since") else None
    except ValueError as exc:
        return SkillResult(ok=False, errors=[str(exc)])

    primitive = params.get("primitive")  # leading intent segment
    actor = params.get("actor")
    decision = params.get("decision")
    federation = params.get("federation_origin")
    raw_limit = params.get("limit")
    limit = int(raw_limit) if raw_limit is not None else _DEFAULT_LIMIT
    if limit < 1 or limit > _MAX_LIMIT:
        return SkillResult(
            ok=False,
            errors=[f"--limit must be 1..{_MAX_LIMIT}, got {limit}"],
        )

    # --- query ------------------------------------------------------------
    with _resolve_session(params) as session:
        q = session.query(Verdict)
        if since_dt is not None:
            q = q.filter(Verdict.decided_at >= since_dt)
        if primitive:
            # Match either exact 'notification' or prefix 'notification.*'.
            q = q.filter(
                (Verdict.intent == primitive)
                | (Verdict.intent.like(f"{primitive}.%"))
            )
        if actor:
            q = q.filter(Verdict.actor == actor)
        if decision:
            q = q.filter(Verdict.decision == decision)
        if federation:
            q = q.filter(Verdict.federation_origin == federation)

        q = q.order_by(Verdict.decided_at.desc()).limit(limit)
        rows = [_to_row(v) for v in q.all()]

    return SkillResult(
        ok=True,
        value={
            "resource": "verdicts",
            "count": len(rows),
            "limit": limit,
            "filters": {
                "since": params.get("since"),
                "primitive": primitive,
                "actor": actor,
                "decision": decision,
                "federation_origin": federation,
            },
            "items": rows,
        },
    )
