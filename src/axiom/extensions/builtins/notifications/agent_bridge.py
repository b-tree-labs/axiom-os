# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Agent-bus → HERALD bridge.

The wire that connects every agent on the platform (RIVET, TIDY, SCAN,
PULSE, future agents) to the human-channel surface (Slack, Mattermost,
Teams, Email, inbox). Before this module, lifecycle events emitted on
``axiom.infra.bus.EventBus`` had nowhere to go — RIVET would publish
``rivet.ci_recovered`` and TIDY would subscribe but no operator-facing
notification fired.

Architecture invariants:

- **Bridge does no routing of its own.** Classification routing lives
  in ``ChannelAdapterRegistry.admitted_for`` (per spec §4); the bridge
  just turns one bus event into one ``send()`` call.
- **Bridge does not own persistence.** Receipts and dedup live on the
  ``SendContext`` (per fabric §6.1); the bridge supplies a stable
  ``dedup_key`` and the rest is downstream.
- **Bridge swallows downstream failures.** An agent emitting a signal
  must never be punished for HERALD's bad day; this is the same
  resilience contract RIVET's lifecycle_events.py adopts.

Configuration is a list of :class:`BridgeRule` rows mapping a subject
pattern to (summary template, priority, classification, actor,
recipient). The first matching rule wins. :func:`default_routing` ships
a starter set covering the well-known agent subjects.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from axiom.extensions.builtins.notifications.send import (
    NotificationPayload,
    Priority,
    SendContext,
    send,
)
from axiom.governance import Classification
from axiom.infra.bus import EventBus
from axiom.infra.bus.types import Subscription

log = logging.getLogger("axiom.notifications.agent_bridge")


# ---------------------------------------------------------------------------
# Routing primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BridgeRule:
    """One subject-pattern → send() mapping.

    Templates use ``{field}`` syntax over the event payload, plus the
    reserved variable ``{_subject}`` that always resolves to the event
    subject (so a wildcard rule doesn't need every emitter to echo the
    subject in payload).
    """

    subject_pattern: str
    summary_template: str = "{_subject}"
    priority: Priority = Priority.NORMAL
    classification: Classification = Classification.INTERNAL
    actor: str = "@agent"
    recipient: str = "@operator"
    intent: str = "notification.send"


@dataclass
class BridgeRouting:
    """Ordered rules; first match wins. Treats ``*`` segments as wildcards
    per NATS subject conventions (matches one segment)."""

    rules: list[BridgeRule] = field(default_factory=list)

    def find(self, subject: str) -> BridgeRule | None:
        for rule in self.rules:
            if _subject_matches(subject, rule.subject_pattern):
                return rule
        return None


def _subject_matches(subject: str, pattern: str) -> bool:
    """NATS-shape segment match. ``*`` matches one segment; literals
    must be exact. Greater-than (``>``) tail-match is not implemented
    here (the bridge defers to the bus's own pattern matcher when it
    subscribes; this helper only resolves the post-hoc rule lookup)."""
    if pattern == subject:
        return True
    pat_segs = pattern.split(".")
    sub_segs = subject.split(".")
    if len(pat_segs) != len(sub_segs):
        return False
    for p, s in zip(pat_segs, sub_segs):
        if p != "*" and p != s:
            return False
    return True


# ---------------------------------------------------------------------------
# Default routing — well-known agent subjects
# ---------------------------------------------------------------------------


def default_routing() -> BridgeRouting:
    """Sensible defaults covering the agent subjects already in flight.

    Operators override by passing a custom :class:`BridgeRouting` to
    :class:`AgentBridge`. Don't edit this in place when tuning your
    deployment — extend it.
    """
    return BridgeRouting(rules=[
        # RIVET lifecycle (release/lifecycle_events.py).
        BridgeRule(
            subject_pattern="rivet.ci_recovered",
            summary_template="✅ CI recovered on {repo}",
            priority=Priority.NORMAL,
            actor="@rivet",
        ),
        BridgeRule(
            subject_pattern="rivet.pr_merged",
            summary_template="merged PR #{pr_number}",
            priority=Priority.LOW,
            actor="@rivet",
        ),
        BridgeRule(
            subject_pattern="rivet.tag_released",
            summary_template="released {tag}",
            priority=Priority.NORMAL,
            actor="@rivet",
        ),
        # TIDY signals (hygiene/subscriber.py).
        BridgeRule(
            subject_pattern="tidy.escalation",
            summary_template="🛑 TIDY escalation: {detail}",
            priority=Priority.HIGH,
            actor="@tidy",
        ),
        # Generic escalation wildcard — any agent's *.escalation.
        BridgeRule(
            subject_pattern="*.escalation",
            summary_template="{_subject}: {detail}",
            priority=Priority.HIGH,
        ),
        # Generic failure wildcard — any agent's *.failed.
        BridgeRule(
            subject_pattern="*.failed",
            summary_template="{_subject}",
            priority=Priority.HIGH,
        ),

        # publishing.* (ADR-059 + ADR-060): PRESS emits events; this
        # bridge routes them through HERALD's channels per recipient
        # preferences, replacing the now-retired
        # ``publishing.providers.notification`` stack.
        BridgeRule(
            subject_pattern="publishing.succeeded",
            summary_template="📄 published {source}",
            priority=Priority.LOW,
            actor="@press",
        ),
        BridgeRule(
            subject_pattern="publishing.draft_ready",
            summary_template="📝 draft ready for review: {source}",
            priority=Priority.NORMAL,
            actor="@press",
        ),
        BridgeRule(
            subject_pattern="publishing.failed",
            summary_template="✗ publishing failed: {source} — {error}",
            priority=Priority.HIGH,
            actor="@press",
        ),

        # rivet.notification (ADR-060): RIVET pr_check_responder emits
        # this instead of importing publishing's TerminalNotificationProvider.
        BridgeRule(
            subject_pattern="rivet.notification",
            summary_template="{summary}",
            priority=Priority.NORMAL,
            actor="@rivet",
        ),
    ])


# ---------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------


class AgentBridge:
    """Wire one ``EventBus`` to one ``SendContext`` via a routing table.

    Lifecycle:

      bridge = AgentBridge(send_ctx, routing=default_routing())
      bridge.attach(bus)   # subscribe each rule
      ...                  # events flow to HERALD
      bridge.detach()      # clean unsubscribe
    """

    def __init__(
        self,
        send_ctx: SendContext,
        routing: BridgeRouting | None = None,
    ) -> None:
        self._ctx = send_ctx
        self._routing = routing or default_routing()
        self.subscriptions: list[Subscription] = []

    def attach(self, bus: EventBus) -> None:
        """Subscribe each rule's subject_pattern; idempotent re-attach
        would double-subscribe so callers should call detach() first."""
        for rule in self._routing.rules:
            sub = bus.subscribe(
                rule.subject_pattern,
                self._make_handler(rule),
                # "warn" so a single handler explosion doesn't sink the
                # bus (per bus FailMode semantics).
                fail_mode="warn",
                source="notifications.agent_bridge",
            )
            self.subscriptions.append(sub)

    def detach(self) -> None:
        """Unsubscribe everything attached."""
        for sub in self.subscriptions:
            try:
                sub.unsubscribe()
            except Exception:  # noqa: BLE001
                pass
        self.subscriptions = []

    # -- internals -----------------------------------------------------

    def _make_handler(self, rule: BridgeRule):
        """Closure so each subscription carries its own rule binding."""

        def handler(subject: str, payload: dict[str, Any]) -> None:
            # Best-effort rule resolution — if multiple rules subscribe
            # the same subject (operator extends defaults), find() returns
            # the first match so behavior is deterministic.
            chosen = self._routing.find(subject) or rule
            summary = _render_summary(chosen.summary_template, subject, payload)
            dedup_key = _dedup_key(subject, payload)
            try:
                send(
                    self._ctx,
                    actor=chosen.actor,
                    recipient=chosen.recipient,
                    payload=NotificationPayload(summary=summary),
                    classification=chosen.classification,
                    priority=chosen.priority,
                    intent=chosen.intent,
                    dedup_key=dedup_key,
                )
            except Exception as exc:  # noqa: BLE001 — resilience contract
                # The agent emitting the signal must never be punished
                # for HERALD's bad day. Log + move on.
                log.warning(
                    "agent_bridge: send() failed for subject=%s: %s",
                    subject, exc,
                )

        return handler


# ---------------------------------------------------------------------------
# Template rendering + dedup
# ---------------------------------------------------------------------------


def _render_summary(
    template: str, subject: str, payload: dict[str, Any]
) -> str:
    """Render the summary; missing fields fall back to the subject string."""
    context = {**payload, "_subject": subject}
    try:
        return template.format(**context)
    except (KeyError, IndexError):
        # Missing field — fall back without crashing the bridge.
        return f"{subject} (template missing fields)"


def _dedup_key(subject: str, payload: dict[str, Any]) -> str:
    """Stable hash of subject + payload for dedup at the send() layer.

    The bridge does not enforce dedup; the SendContext does (fabric §6.1).
    Bridge only supplies a key that is stable across re-emissions of the
    same logical event.
    """
    blob = subject + "|" + json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


__all__ = [
    "AgentBridge",
    "BridgeRouting",
    "BridgeRule",
    "default_routing",
]
