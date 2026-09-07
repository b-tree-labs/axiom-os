# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``audit.causes`` — list verdicts caused by a given fragment (PRD §5.4).

Mirror image of ``chain``: ``chain`` walks *up* from a verdict to its
root; ``causes`` walks *down* from a fragment to every verdict that
named it as its provenance parent. Useful for "what did this approval
authorize downstream?".
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..db_models import Verdict
from .list_verdicts import _resolve_session, _to_row

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 500


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    fragment_id = params.get("fragment_id") or ""
    if not fragment_id:
        return SkillResult(
            ok=False, errors=["fragment_id is required (positional arg)"],
        )

    raw_limit = params.get("limit")
    limit = int(raw_limit) if raw_limit is not None else _DEFAULT_LIMIT
    if limit < 1 or limit > _MAX_LIMIT:
        return SkillResult(
            ok=False, errors=[f"--limit must be 1..{_MAX_LIMIT}, got {limit}"],
        )

    with _resolve_session(params) as session:
        rows = (
            session.query(Verdict)
            .filter(Verdict.provenance_parent == fragment_id)
            .order_by(Verdict.decided_at.desc())
            .limit(limit)
            .all()
        )
        items = [_to_row(v) for v in rows]

    return SkillResult(
        ok=True,
        value={
            "resource": "causes",
            "fragment_id": fragment_id,
            "count": len(items),
            "limit": limit,
            "items": items,
        },
    )
