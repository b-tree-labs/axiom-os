# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""TRIAGE inbound subscriber — bus wiring (ADR-067 §6).

Subscribes to ``herald.inbound.>`` and runs the deterministic classifier
on each event. A resolved target emits ``herald.dispatch.<agent>`` (the
seam SkillRegistry dispatch + outbound reply wire onto at serve time); an
unresolved one emits ``herald.reply`` carrying the below-floor prompt.
TRIAGE keeps its existing diagnostics role; this is the second role
ADR-067 adds, not a new agent.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from axiom.extensions.builtins.notifications.gateway.classify import classify_inbound
from axiom.extensions.builtins.notifications.gateway.threads import ThreadStore

_INBOUND_PATTERN = "herald.inbound.>"
_DISPATCH_SOURCE = "triage.classify_inbound"


class _BusLike:  # documentation alias; real bus is axiom.infra.bus.EventBus
    def subscribe(self, pattern: str, handler: Any) -> Any: ...
    def publish(
        self, subject: str, payload: dict[str, Any] | None = ..., source: str = ...
    ) -> Any: ...


def register_triage(
    bus: Any,
    *,
    known_agents: Iterable[str],
    threads: ThreadStore | None = None,
) -> Any:
    """Wire TRIAGE's inbound classifier onto the bus. Returns the subscription."""
    known = list(known_agents)

    def _on_inbound(subject: str, payload: dict[str, Any]) -> None:
        text = str(payload.get("text") or "")
        decision = classify_inbound(text, known_agents=known, threads=threads)
        if decision.target_principal is not None:
            agent = decision.target_principal.lstrip("@").split(":", 1)[0]
            bus.publish(
                f"herald.dispatch.{agent}",
                payload={
                    "target": decision.target_principal,
                    "reason": decision.reason,
                    "confidence": decision.confidence,
                    "text": text,
                    "vendor": payload.get("vendor"),
                    "sender_ref": payload.get("sender_ref"),
                    "thread_ref": payload.get("thread_ref"),
                    "channel": payload.get("channel"),
                },
                source=_DISPATCH_SOURCE,
            )
        else:
            bus.publish(
                "herald.reply",
                payload={
                    "reply_text": decision.reply_text,
                    "vendor": payload.get("vendor"),
                    "thread_ref": payload.get("thread_ref"),
                    "channel": payload.get("channel"),
                    "reason": decision.reason,
                },
                source=_DISPATCH_SOURCE,
            )

    return bus.subscribe(_INBOUND_PATTERN, _on_inbound)


__all__ = ["register_triage"]
