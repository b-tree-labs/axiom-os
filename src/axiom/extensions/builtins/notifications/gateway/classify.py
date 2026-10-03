# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""TRIAGE inbound classifier — deterministic mode (ADR-067 §2, §6).

Resolves an inbound message to a target agent without an LLM:

1. **@mention** (always first, deterministic): the first ``@name`` in the
   text that matches a known agent wins.
2. **thread-context**: if no mention, recover the reply-bind-back token and
   route to the agent that sent the original notification.
3. **below floor**: neither resolved → ask the operator to @-mention an
   agent. The LLM intent classifier (Phase 1.5) slots in here; the
   deterministic stub never guesses.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from axiom.chat.addressing import parse_mentions
from axiom.extensions.builtins.notifications.gateway.threads import (
    ThreadStore,
    parse_token,
)

_BELOW_FLOOR_REPLY = (
    "I couldn't tell which agent you meant. @-mention one (e.g. `@rivet`) "
    "and I'll route it."
)


@dataclass(frozen=True)
class Decision:
    target_principal: str | None
    reason: str  # "mention" | "thread" | "below_floor"
    confidence: float
    reply_text: str | None = None  # set only when below floor


def _agent_name(handle: str) -> str:
    return handle.lstrip("@").split(":", 1)[0].lower()


def classify_inbound(
    text: str,
    *,
    known_agents: Iterable[str],
    threads: ThreadStore | None = None,
) -> Decision:
    known = {a.lower() for a in known_agents}

    # 1. @mention — deterministic, always wins when present.
    for handle in parse_mentions(text or ""):
        name = _agent_name(handle)
        if name in known:
            return Decision(
                target_principal=f"@{name}", reason="mention", confidence=1.0
            )

    # 2. thread-context via the reply-bind-back token.
    if threads is not None:
        token = parse_token(text or "")
        if token:
            rec = threads.by_token(token)
            if rec is not None:
                return Decision(
                    target_principal=rec.actor, reason="thread", confidence=0.9
                )

    # 3. below floor — never guess.
    return Decision(
        target_principal=None,
        reason="below_floor",
        confidence=0.0,
        reply_text=_BELOW_FLOOR_REPLY,
    )


__all__ = ["Decision", "classify_inbound"]
