# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""HERALD alerts for Actions storage crossings. Modeled on RIVET's ci_health alerts."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass

from axiom.extensions.builtins.hygiene.storage_watch import ThresholdCrossing

_log = logging.getLogger("axiom.hygiene.storage")

ACTOR = "@tidy"


@dataclass(frozen=True)
class Alert:
    kind: str
    severity: str
    summary: str
    action_taken: str
    decision: str
    dedup_key: str
    detail: str = ""

    def body(self) -> str:
        text = f"{self.summary}\n\nTIDY did: {self.action_taken}\nYou decide: {self.decision}"
        return f"{text}\n\n{self.detail}" if self.detail else text

    def to_dict(self) -> dict:
        return asdict(self)


Sender = Callable[[str, Alert], str | None]


def from_crossing(crossing: ThresholdCrossing, *, watch_only: bool, action_taken: str) -> Alert:
    severity = "urgent" if crossing.severity == "escalate" else "high"
    decision = (
        "This run stayed dry-run. TIDY acts in policy by default unless the kill "
        "switch (hygiene.storage_enabled=false) or --dry-run is set."
        if watch_only
        else (
            "Review the audit log if a delete looks wrong. Re-run the source workflow "
            "to regenerate a dist; caches rebuild on the next CI run. Billing plan "
            "changes stay with you."
        )
    )
    return Alert(
        kind=crossing.kind,
        severity=severity,
        summary=crossing.summary,
        action_taken=action_taken,
        decision=decision,
        dedup_key=f"tidy.storage.{crossing.kind}.{crossing.repo or 'org'}.{crossing.severity}",
        detail="",
    )


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
            intent=f"hygiene.storage.{alert.kind}",
            dedup_key=alert.dedup_key,
        )
        return receipt.id
    except Exception as exc:  # noqa: BLE001 - an alert never fails the sweep
        _log.warning("storage alert failed for %s: %s", alert.kind, exc)
        return None


def send_alerts(
    alerts: list[Alert],
    recipients: tuple[str, ...],
    *,
    already_sent: set[str],
    sender: Sender | None = None,
) -> list[dict]:
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
