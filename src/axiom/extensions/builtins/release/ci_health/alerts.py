# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CI-health alerts: one action RIVET took, one decision left to a person.

Alerts go out through HERALD (the notifications extension) to each configured
recipient. HERALD writes the inbox row and routes to the recipient's channel.
A failed send is logged and never fails the sweep.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass

_log = logging.getLogger("axiom.release.ci_health")

ACTOR = "@rivet"


@dataclass(frozen=True)
class Alert:
    kind: str
    severity: str  # "high" | "urgent" | "normal"
    summary: str
    action_taken: str
    decision: str
    dedup_key: str
    detail: str = ""

    def body(self) -> str:
        text = f"{self.summary}\n\nRIVET did: {self.action_taken}\nYou decide: {self.decision}"
        return f"{text}\n\n{self.detail}" if self.detail else text

    def to_dict(self) -> dict:
        return asdict(self)


Sender = Callable[[str, Alert], str | None]


def herald_sender(recipient: str, alert: Alert) -> str | None:
    """Send one alert through HERALD. Returns the receipt id, or None."""
    try:
        from axiom.extensions.builtins.notifications.send import NotificationPayload, Priority, send
        from axiom.extensions.builtins.notifications.skills.send import _ctx
        from axiom.governance import Classification

        receipt = send(
            _ctx(),
            actor=ACTOR,
            recipient=recipient,
            payload=NotificationPayload(
                summary=alert.summary,
                body=alert.body(),
                metadata={"kind": alert.kind, "action_taken": alert.action_taken, "decision": alert.decision},
            ),
            classification=Classification.INTERNAL,
            priority=Priority(alert.severity if alert.severity in {"low", "normal", "high", "urgent"} else "high"),
            intent=f"release.ci_health.{alert.kind}",
            dedup_key=alert.dedup_key,
        )
        return receipt.id
    except Exception as exc:  # noqa: BLE001 - an alert never fails the sweep
        _log.warning("ci-health alert failed for %s: %s", alert.kind, exc)
        return None


def send_alerts(
    alerts: list[Alert],
    recipients: tuple[str, ...],
    *,
    already_sent: set[str],
    sender: Sender | None = None,
) -> list[dict]:
    """Send each alert not already sent under its dedup key."""
    sender = sender or herald_sender
    out: list[dict] = []
    for alert in alerts:
        if alert.dedup_key in already_sent:
            out.append({**alert.to_dict(), "sent": False, "why": "already sent"})
            continue
        receipts = [sender(recipient, alert) for recipient in recipients]
        sent = any(receipts)
        if sent:
            already_sent.add(alert.dedup_key)
        out.append({**alert.to_dict(), "sent": sent, "receipts": [r for r in receipts if r]})
    return out
