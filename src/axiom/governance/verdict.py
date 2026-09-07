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

#: RFC 9470 error codes. Anything else is a typo, not a new protocol.
_CHALLENGE_ERRORS = ("insufficient_user_authentication", "insufficient_scope")


class Decision(str, Enum):
    PERMIT = "permit"
    DENY = "deny"
    PROPOSE_TO_HUMAN = "propose_to_human"
    RATE_LIMIT = "rate_limit"
    EXPIRED_CAPABILITY = "expired_capability"
    STEP_UP_REQUIRED = "step_up_required"


class NextAction(str, Enum):
    """What the caller does next, derived from the decision.

    PROCEED          — caller continues with the action.
    ABORT            — caller stops; do not retry without a new envelope.
    ENQUEUE_PROPOSAL — caller queues a proposal for human approval (HERALD).
    AWAIT_HUMAN      — caller blocks for an explicit human ack
                       (rare; reserved for synchronous-UX call sites).
    SATISFY_CHALLENGE— caller elevates (re-authenticate, or re-resolve a stale
                       actor) and retries. Deliberately NOT ABORT: step-up is
                       recoverable, and collapsing it into ABORT would make an
                       elevation prompt indistinguishable from a denial.
    """

    PROCEED = "proceed"
    ABORT = "abort"
    ENQUEUE_PROPOSAL = "enqueue_proposal"
    AWAIT_HUMAN = "await_human"
    SATISFY_CHALLENGE = "satisfy_challenge"


_DECISION_TO_NEXT_ACTION: dict[Decision, NextAction] = {
    Decision.PERMIT: NextAction.PROCEED,
    Decision.DENY: NextAction.ABORT,
    Decision.PROPOSE_TO_HUMAN: NextAction.ENQUEUE_PROPOSAL,
    Decision.RATE_LIMIT: NextAction.ABORT,
    Decision.EXPIRED_CAPABILITY: NextAction.ABORT,
    Decision.STEP_UP_REQUIRED: NextAction.SATISFY_CHALLENGE,
}


@dataclass(frozen=True)
class Challenge:
    """What the caller must satisfy to proceed — RFC 9470 (ADR-084).

    Two uses, one shape: elevate the authentication (``acr_values``), or refresh
    a too-old actor (``max_age``) — which is how ADR-103 decision 6 asks for a
    fresher projection without performing a lookup inside ``decide()``.
    """

    error: str = "insufficient_user_authentication"
    acr_values: tuple[str, ...] = ()
    max_age: float | None = None
    scope: str | None = None
    remediation: str | None = None
    """Human-facing next step, e.g. the exact command to run."""

    def __post_init__(self) -> None:
        if self.error not in _CHALLENGE_ERRORS:
            raise ValueError(
                f"unknown challenge error {self.error!r}; "
                f"expected one of {', '.join(_CHALLENGE_ERRORS)}"
            )

    def to_www_authenticate(self) -> str:
        """Render the RFC 9470 ``WWW-Authenticate`` header value."""
        parts = [f'error="{self.error}"']
        if self.acr_values:
            parts.append(f'acr_values="{" ".join(self.acr_values)}"')
        if self.max_age is not None:
            # Emitted even when 0 — "authenticate right now" is the strictest
            # possible demand, and dropping it as falsy would silently void it.
            parts.append(f"max_age={int(self.max_age)}")
        if self.scope:
            parts.append(f'scope="{self.scope}"')
        return "Bearer " + ", ".join(parts)


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    reason: str
    receipt_fragment_id: str
    next_action_for_caller: NextAction
    challenge: Challenge | None = None
    """Present iff ``decision is STEP_UP_REQUIRED`` — enforced both ways."""

    def __post_init__(self) -> None:
        if not self.reason:
            raise ValueError("Verdict.reason cannot be empty")
        if not self.receipt_fragment_id:
            raise ValueError("Verdict.receipt_fragment_id cannot be empty")
        step_up = self.decision is Decision.STEP_UP_REQUIRED
        if step_up and self.challenge is None:
            raise ValueError(
                "STEP_UP_REQUIRED requires a Challenge — a caller told to step up "
                "but not told how cannot proceed"
            )
        if not step_up and self.challenge is not None:
            raise ValueError(
                f"a Challenge is meaningless on {self.decision.value!r}; "
                "it would be decorative and callers would learn to ignore it"
            )

    @property
    def is_permitted(self) -> bool:
        return self.decision is Decision.PERMIT

    @classmethod
    def from_decision(
        cls,
        decision: Decision,
        reason: str,
        receipt_fragment_id: str,
        *,
        challenge: Challenge | None = None,
    ) -> Verdict:
        return cls(
            decision=decision,
            reason=reason,
            receipt_fragment_id=receipt_fragment_id,
            next_action_for_caller=_DECISION_TO_NEXT_ACTION[decision],
            challenge=challenge,
        )


__all__ = ["Challenge", "Decision", "NextAction", "Verdict"]
