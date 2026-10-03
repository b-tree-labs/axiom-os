# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``notifications.send`` skill — wraps the ``send()`` façade per ADR-056."""

from __future__ import annotations

from typing import Any

from axiom.extensions.builtins.notifications.send import (
    NotificationPayload,
    Priority,
    SendContext,
)
from axiom.extensions.builtins.notifications.send import (
    send as _send,
)
from axiom.governance import Classification
from axiom.infra.skills import SkillContext, SkillResult

# SEC-1: a single process-level SendContext.default() so the CLI
# subprocess smokes share inbox state across `send` then `list`.
_CTX: SendContext | None = None


def _ctx() -> SendContext:
    global _CTX
    if _CTX is None:
        _CTX = SendContext.default()
    return _CTX


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    recipient = params.get("recipient")
    summary = params.get("summary")
    if not recipient:
        return SkillResult(ok=False, errors=["missing required param: recipient"])
    if not summary:
        return SkillResult(ok=False, errors=["missing required param: summary"])

    try:
        classification = Classification.from_str(
            params.get("classification") or "internal"
        )
    except ValueError as exc:
        return SkillResult(ok=False, errors=[str(exc)])

    try:
        priority = Priority(params.get("priority") or "normal")
    except ValueError:
        return SkillResult(
            ok=False,
            errors=[f"unknown priority: {params.get('priority')!r}"],
        )

    receipt = _send(
        _ctx(),
        actor=params.get("actor") or "@cli:local",
        recipient=recipient,
        payload=NotificationPayload(summary=summary, body=params.get("body")),
        classification=classification,
        priority=priority,
        intent=params.get("intent") or "notification.send",
        dedup_key=params.get("dedup_key"),
    )

    return SkillResult(
        ok=receipt.outcome == "succeeded",
        value={
            "receipt_id": receipt.id,
            "outcome": receipt.outcome,
            "channel_selected": receipt.channel_selected,
            "correlation_id": receipt.correlation_id,
            "routing_rationale": receipt.routing_rationale,
            "error": receipt.error,
        },
        actions_taken=[
            f"send → {recipient} via {receipt.channel_selected or '(none)'}: "
            f"{receipt.outcome}"
        ],
        errors=[receipt.error] if receipt.error else [],
    )


__all__ = ["run"]
