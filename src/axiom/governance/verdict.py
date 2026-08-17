# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Verdict — GUARD's typed answer to `decide(envelope)`.

Per spec-governance-fabric §5.1 + prd-axiom-authz §5.1: the verdict
returns a `decision` (permit / deny / propose_to_human / rate_limit /
expired_capability), the canonical `reason`, the receipt fragment id (so
auditors can find the full decision context), and the
`next_action_for_caller` field which is the API every caller branches on.

No caller should inspect the `decision` field directly; the typed
next-action is what the call-site contract expects.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Decision(str, Enum):
    PERMIT = "permit"
    DENY = "deny"
    PROPOSE_TO_HUMAN = "propose_to_human"
    RATE_LIMIT = "rate_limit"
    EXPIRED_CAPABILITY = "expired_capability"


class NextAction(str, Enum):
    """What the caller does next, derived from the decision.

    PROCEED          — caller continues with the action.
    ABORT            — caller stops; do not retry without a new envelope.
    ENQUEUE_PROPOSAL — caller queues a proposal for human approval (HERALD).
    AWAIT_HUMAN      — caller blocks for an explicit human ack
                       (rare; reserved for synchronous-UX call sites).
    """

    PROCEED = "proceed"
    ABORT = "abort"
    ENQUEUE_PROPOSAL = "enqueue_proposal"
    AWAIT_HUMAN = "await_human"


_DECISION_TO_NEXT_ACTION: dict[Decision, NextAction] = {
    Decision.PERMIT: NextAction.PROCEED,
    Decision.DENY: NextAction.ABORT,
    Decision.PROPOSE_TO_HUMAN: NextAction.ENQUEUE_PROPOSAL,
    Decision.RATE_LIMIT: NextAction.ABORT,
    Decision.EXPIRED_CAPABILITY: NextAction.ABORT,
}


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    reason: str
    receipt_fragment_id: str
    next_action_for_caller: NextAction

    def __post_init__(self) -> None:
        if not self.reason:
            raise ValueError("Verdict.reason cannot be empty")
        if not self.receipt_fragment_id:
            raise ValueError("Verdict.receipt_fragment_id cannot be empty")

    @property
    def is_permitted(self) -> bool:
        return self.decision is Decision.PERMIT

    @classmethod
    def from_decision(
        cls, decision: Decision, reason: str, receipt_fragment_id: str
    ) -> Verdict:
        return cls(
            decision=decision,
            reason=reason,
            receipt_fragment_id=receipt_fragment_id,
            next_action_for_caller=_DECISION_TO_NEXT_ACTION[decision],
        )


__all__ = ["Decision", "NextAction", "Verdict"]
