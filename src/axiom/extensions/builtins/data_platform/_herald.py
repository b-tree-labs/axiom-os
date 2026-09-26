# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Best-effort HERALD publishing for data-platform lifecycle events.

The data-platform's operational events (``data.backup.failed``,
``data.backup.stale``, ``data.backup.validate_failed``, …) must reach an
operator, not just a log file. This helper wraps HERALD's programmatic
:func:`notifications.send.send` façade — the same internal function
``axi notifications send`` calls — with the agent-bridge resilience
contract: **the emitting skill is never punished for HERALD's bad day**
(failures are logged and swallowed).

Each event is also published best-effort on the default EventBus
(ADR-060) so cross-agent subscribers can react without coupling —
mirroring how PULSE's hooks emit.
"""

from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger("axiom.data_platform.herald")

_ACTOR = "@plinth"
_DEFAULT_RECIPIENT = "@operator"


def publish_event(
    intent: str,
    summary: str,
    *,
    body: str | None = None,
    priority: str = "high",
    recipient: str | None = None,
    dedup_key: str | None = None,
    payload: dict[str, Any] | None = None,
) -> str | None:
    """Publish one operational event through HERALD (+ EventBus best-effort).

    Returns the delivery-receipt id, or ``None`` when the send could not
    be attempted/completed. NEVER raises.
    """
    _publish_bus(intent, {**(payload or {}), "summary": summary})
    try:
        from axiom.extensions.builtins.notifications.send import (
            NotificationPayload,
            Priority,
            send,
        )
        from axiom.extensions.builtins.notifications.skills.send import _ctx
        from axiom.governance import Classification

        receipt = send(
            _ctx(),
            actor=_ACTOR,
            recipient=recipient or _DEFAULT_RECIPIENT,
            payload=NotificationPayload(summary=summary, body=body),
            classification=Classification.INTERNAL,
            priority=Priority(priority),
            intent=intent,
            dedup_key=dedup_key,
        )
        return receipt.id
    except Exception as exc:  # noqa: BLE001 — resilience contract
        _log.warning("herald publish failed for %s: %s", intent, exc)
        return None


def _publish_bus(subject: str, payload: dict[str, Any]) -> None:
    """Best-effort EventBus publish; advisory, never raises."""
    try:
        from axiom.infra.bus import get_default_eventbus

        get_default_eventbus().publish(subject, payload, source="data_platform")
    except Exception:  # noqa: BLE001 — signalling is advisory
        pass


__all__ = ["publish_event"]
