# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``audit.show`` — fetch one verdict receipt by id (PRD §5.4).

CLI shape::

    axi audit show <receipt-id> [--json]

Surfaces every column verbatim so the operator can reason about a
single decision without piecing together log lines. Use
``axi audit explain`` (AUTHZ-3) for the narrative ``why``; ``show``
is the raw record.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..db_models import Verdict
from .list_verdicts import _resolve_session, _to_row


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    receipt_id = params.get("receipt_id") or ""
    if not receipt_id:
        return SkillResult(
            ok=False,
            errors=["receipt_id is required (positional arg)"],
        )

    with _resolve_session(params) as session:
        v = session.query(Verdict).filter(Verdict.id == receipt_id).one_or_none()
        if v is None:
            return SkillResult(
                ok=False,
                errors=[f"no verdict found with id={receipt_id!r}"],
            )
        row = _to_row(v)
        # Include the audit-trail columns ``list`` elides for brevity.
        row.update(
            capability_id=v.capability_id,
            context_fragment_id=v.context_fragment_id,
            provenance_parent=v.provenance_parent,
            dedup_key=v.dedup_key,
            matched_rules=v.matched_rules or [],
        )

    return SkillResult(
        ok=True,
        value={
            "resource": "verdict",
            "item": row,
        },
    )
