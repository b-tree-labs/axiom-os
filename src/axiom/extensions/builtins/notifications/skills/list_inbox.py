# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``notifications.list`` skill — query the unified inbox."""

from __future__ import annotations

from typing import Any

from axiom.extensions.builtins.notifications.inbox import InboxQuery
from axiom.extensions.builtins.notifications.inbox_db import default_inbox_store
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

    # READ THE DURABLE STORE, not `_ctx().inbox_store`.
    #
    # That one is in-memory, so `axi notifications list` told an operator
    # "no inbox rows" while chat, reading the database, was showing three
    # alerts for the same principal. Same person, same question, two answers —
    # exactly what one-chat-many-surfaces exists to prevent.
    #
    # `default_inbox_store()` is the seam the extension already had: Postgres
    # when reachable, in-memory when there is genuinely no database, cached
    # once per process. `send` stays on its own pipeline for now — an inbox row
    # has a NOT NULL foreign key to a delivery receipt and nothing persists
    # receipts yet — but a READ carries no such constraint.
    rows = default_inbox_store().query(
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
