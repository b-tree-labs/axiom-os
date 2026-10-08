# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``audit.graduation`` — list RACI graduation state (PRD §5.4 / §5.3).

Shows the ``approvals / threshold`` count + ``graduated`` flag per
``(actor, intent_class, resource_pattern)`` row. Filters by actor or
intent_class; defaults to listing all.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..db_models import Graduation
from .list_verdicts import _resolve_session

_DEFAULT_LIMIT = 100
_MAX_LIMIT = 1000


def _to_row(g: Graduation) -> dict[str, Any]:
    return {
        "id": g.id,
        "actor": g.actor,
        "intent_class": g.intent_class,
        "resource_pattern": g.resource_pattern,
        "approvals": g.approvals,
        "threshold": g.threshold,
        "graduated": g.graduated,
        "last_update": g.last_update.isoformat() if g.last_update else None,
    }


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    actor = params.get("actor")
    intent_class = params.get("intent_class")
    only_graduated = bool(params.get("only_graduated"))
    only_proposing = bool(params.get("only_proposing"))

    if only_graduated and only_proposing:
        return SkillResult(
            ok=False,
            errors=["--only-graduated and --only-proposing are mutually exclusive"],
        )

    raw_limit = params.get("limit")
    limit = int(raw_limit) if raw_limit is not None else _DEFAULT_LIMIT
    if limit < 1 or limit > _MAX_LIMIT:
        return SkillResult(
            ok=False, errors=[f"--limit must be 1..{_MAX_LIMIT}, got {limit}"],
        )

    with _resolve_session(params) as session:
        q = session.query(Graduation)
        if actor:
            q = q.filter(Graduation.actor == actor)
        if intent_class:
            q = q.filter(Graduation.intent_class == intent_class)
        if only_graduated:
            q = q.filter(Graduation.graduated.is_(True))
        if only_proposing:
            q = q.filter(Graduation.graduated.is_(False))
        q = q.order_by(Graduation.actor, Graduation.intent_class).limit(limit)
        items = [_to_row(g) for g in q.all()]

    return SkillResult(
        ok=True,
        value={
            "resource": "graduation",
            "count": len(items),
            "limit": limit,
            "filters": {
                "actor": actor,
                "intent_class": intent_class,
                "only_graduated": only_graduated,
                "only_proposing": only_proposing,
            },
            "items": items,
        },
    )
