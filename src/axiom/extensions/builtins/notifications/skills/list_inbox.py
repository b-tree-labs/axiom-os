# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``notifications.list`` skill — query the unified inbox."""

from __future__ import annotations

from typing import Any

from axiom.extensions.builtins.notifications.inbox import InboxQuery
from axiom.extensions.builtins.notifications.skills.send import _ctx
from axiom.governance import Classification
from axiom.infra.skills import SkillContext, SkillResult


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    recipient = params.get("recipient") or "@cli:local"
    unread_only = bool(params.get("unread_only", False))
    max_class = params.get("max_classification")
    try:
        cls = Classification.from_str(max_class) if max_class else None
    except ValueError as exc:
        return SkillResult(ok=False, errors=[str(exc)])

    rows = _ctx().inbox_store.query(
        InboxQuery(
            recipient=recipient,
            unread_only=unread_only,
            max_classification=cls,
            limit=int(params.get("limit", 50)),
        )
    )
    return SkillResult(
        ok=True,
        value={
            "resource": "inbox",
            "recipient": recipient,
            "count": len(rows),
            "items": [
                {
                    "id": r.id,
                    "receipt_id": r.receipt_id,
                    "classification": r.classification.value,
                    "priority": r.priority,
                    "summary": r.summary,
                    "read": r.read_at is not None,
                    "created_at": r.created_at.isoformat(),
                }
                for r in rows
            ],
        },
    )


__all__ = ["run"]
