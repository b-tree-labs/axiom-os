# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Channel-adapter observability primitives.

Per the 2026-06-01 connector-quality bar §7: every connector emits a
structured outcome event after every send. Operators see what's working,
what's failing, and what needs reconnecting without reading logs.

This module is the publish surface. Adapters call ``publish_outcome``
from their result return path; the status store + agent-bus → HERALD
bridge subscribe from there.

The bus subjects emitted:

- ``connector.delivered`` — success path, every channel
- ``connector.failed`` — non-auth failure (HTTP 4xx/5xx/network) on any
  attempt path
- ``connector.reconnect_required`` — 401/403 short-circuit from any
  adapter; agent-bridge default routing already maps ``*.reconnect_required``
  to high priority so the operator sees it in their inbox + Slack + SMS
  without further configuration

Why subjects, not a typed bus channel: every other agent on the platform
(RIVET, TIDY, SCAN) talks to the bus by subject name (see
``release/lifecycle_events.py``). Keeping the same shape means HERALD
observability falls out of the same telemetry surface — no separate
plane to learn.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol


# ---------------------------------------------------------------------------
# Subject constants
# ---------------------------------------------------------------------------


SUBJECT_DELIVERED = "connector.delivered"
SUBJECT_FAILED = "connector.failed"
SUBJECT_RECONNECT_REQUIRED = "connector.reconnect_required"


# ---------------------------------------------------------------------------
# Result Protocol — duck-type for every adapter's dispatch result
# ---------------------------------------------------------------------------


class _AdapterResult(Protocol):
    """Minimal shape ``publish_outcome`` reads from any adapter result.

    Every adapter (``SlackDispatchResult``, ``MattermostDispatchResult``,
    ``TeamsDispatchResult``, ``TwilioSmsDispatchResult``,
    ``EmailDispatchResult``, ``InboxDispatchResult``) exposes ``ok``;
    most expose the optional fields below.
    """

    ok: bool


# ---------------------------------------------------------------------------
# Publish helper
# ---------------------------------------------------------------------------


log = logging.getLogger("axiom.notifications.observability")


def publish_outcome(
    bus: Any,
    *,
    connector: str,
    result: _AdapterResult,
    recipient: str = "",
    receipt_id: str = "",
) -> None:
    """Publish one ``connector.*`` event for an adapter result.

    Resilient by contract — never raises, even if the bus is missing or
    misbehaving. The adapter side never knows about subscribers; the
    publish call should be the last line of ``deliver_sync``.

    ``bus`` is duck-typed: any object with ``publish(subject, payload,
    source=...)`` works (the platform default ``axiom.infra.bus.EventBus``
    fits; tests inject a stub).
    """
    if bus is None:
        return

    payload = _build_payload(
        connector=connector,
        result=result,
        recipient=recipient,
        receipt_id=receipt_id,
    )
    subject = _pick_subject(result)

    try:
        bus.publish(subject, payload, source=f"connector.{connector}")
    except Exception as exc:  # noqa: BLE001 — observability never breaks the caller
        log.debug(
            "publish_outcome: bus.publish failed for %s/%s: %s",
            connector, subject, exc,
        )


def _pick_subject(result: _AdapterResult) -> str:
    if getattr(result, "reconnect_required", False):
        return SUBJECT_RECONNECT_REQUIRED
    if result.ok:
        return SUBJECT_DELIVERED
    return SUBJECT_FAILED


def _build_payload(
    *,
    connector: str,
    result: _AdapterResult,
    recipient: str,
    receipt_id: str,
) -> dict[str, Any]:
    return {
        "connector": connector,
        "ok": bool(result.ok),
        "recipient": recipient,
        "receipt_id": receipt_id,
        "status_code": getattr(result, "status_code", None),
        "error": getattr(result, "error", None),
        "retry_attempts": getattr(result, "retry_attempts", 0),
        "reconnect_required": getattr(result, "reconnect_required", False),
        # Vendor-specific fields, when present, ride the payload so the
        # status surface can show them.
        "message_id": getattr(
            result, "message_id", getattr(result, "message_sid", None)
        ),
        "vendor_code": getattr(result, "twilio_code", None),
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Outcome dataclass — what the status store stores per connector
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConnectorOutcome:
    """One observation of one connector's last send."""

    connector: str
    ok: bool
    observed_at: datetime
    recipient: str = ""
    status_code: int | None = None
    error: str | None = None
    retry_attempts: int = 0
    reconnect_required: bool = False
    message_id: str | None = None
    vendor_code: int | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ConnectorOutcome":
        """Build from a bus payload (round-trip safe)."""
        ts_raw = payload.get("observed_at")
        try:
            observed_at = (
                datetime.fromisoformat(ts_raw)
                if ts_raw
                else datetime.now(timezone.utc)
            )
        except (TypeError, ValueError):
            observed_at = datetime.now(timezone.utc)
        return cls(
            connector=payload.get("connector", "?"),
            ok=bool(payload.get("ok", False)),
            observed_at=observed_at,
            recipient=payload.get("recipient", ""),
            status_code=payload.get("status_code"),
            error=payload.get("error"),
            retry_attempts=int(payload.get("retry_attempts", 0)),
            reconnect_required=bool(payload.get("reconnect_required", False)),
            message_id=payload.get("message_id"),
            vendor_code=payload.get("vendor_code"),
        )


__all__ = [
    "ConnectorOutcome",
    "SUBJECT_DELIVERED",
    "SUBJECT_FAILED",
    "SUBJECT_RECONNECT_REQUIRED",
    "publish_outcome",
]
