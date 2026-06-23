# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""decide() — the single GUARD decision API.

Per prd-axiom-authz §5.1: every action that crosses an authorization
boundary calls `decide(envelope) → Verdict` exactly once. The verdict's
`next_action_for_caller` is the caller-side contract; callers never
inspect the raw decision.

This module is the public-facing API. Internally it composes:

1. The `RuleEngine` (per §5.2) for explicit per-resource per-intent rules.
2. The graduation layer (per §5.3) for novel-action defaults.
3. The receipt writer (per spec §4) — every decide() call produces a
   provenance-stamped audit fragment via `session_for('authz')`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from axiom.extensions.builtins.authz.db_models import (
    Graduation,
    Verdict as VerdictRow,
)
from axiom.extensions.builtins.authz.rules import (
    Rule,
    RuleEngine,
)
from axiom.governance import (
    ActionEnvelope,
    Decision,
    Verdict,
)


@dataclass
class DecideContext:
    """Per-call context. In production a singleton; in tests, scoped.

    Holds the active rule engine + a session factory (typically
    `axiom.infra.db.session_for('authz')`) plus the default graduation
    threshold.
    """

    rule_engine: RuleEngine = field(default_factory=RuleEngine)
    session_factory: object | None = None
    """Callable returning a context manager yielding a `Session`.

    Production: `lambda: session_for('authz')`. Tests pass a fake.
    """
    default_threshold: int = 5

    def add_rule(self, rule: Rule) -> None:
        self.rule_engine.add(rule)


def decide(envelope: ActionEnvelope, ctx: DecideContext) -> Verdict:
    """The single decision API. Returns a typed Verdict.

    The decision pipeline:

    1. Capability lifecycle check — expired token → ``EXPIRED_CAPABILITY``.
    2. Rule engine — explicit rules win when they match.
    3. Graduation — for novel action classes, ``PROPOSE_TO_HUMAN``;
       after N approvals, ``PERMIT``.
    4. Default — fail-closed ``DENY`` (lint catches missing graduation
       seeds).

    Every path writes a receipt.
    """
    receipt_id = _new_receipt_id()
    now = datetime.now(timezone.utc)

    # Step 1: capability lifecycle.
    if not envelope.capability.is_valid_at(now):
        return _emit(
            envelope=envelope,
            ctx=ctx,
            receipt_id=receipt_id,
            decision=Decision.EXPIRED_CAPABILITY,
            reason="capability not valid at decision time",
            matched_rules=(),
        )

    # Step 1b: capability scope check — token must permit this verb +
    # this resource at this classification.
    if not envelope.capability.permits_intent(envelope.intent):
        return _emit(
            envelope=envelope,
            ctx=ctx,
            receipt_id=receipt_id,
            decision=Decision.DENY,
            reason="capability does not permit this intent",
            matched_rules=(),
        )
    if not envelope.capability.permits_resource(envelope.resource):
        return _emit(
            envelope=envelope,
            ctx=ctx,
            receipt_id=receipt_id,
            decision=Decision.DENY,
            reason="capability does not permit this resource",
            matched_rules=(),
        )
    if not envelope.capability.permits_classification(envelope.classification):
        return _emit(
            envelope=envelope,
            ctx=ctx,
            receipt_id=receipt_id,
            decision=Decision.DENY,
            reason="capability classification ceiling exceeded",
            matched_rules=(),
        )

    # Step 2: rule engine.
    combined = ctx.rule_engine.evaluate(envelope, now)
    if combined.disposition is not None:
        decision = _disposition_to_decision(combined.disposition)
        return _emit(
            envelope=envelope,
            ctx=ctx,
            receipt_id=receipt_id,
            decision=decision,
            reason=f"matched rules: {', '.join(combined.matched_rules)}",
            matched_rules=combined.matched_rules,
        )

    # Step 3: graduation.
    graduated = _is_graduated(envelope, ctx)
    if graduated:
        return _emit(
            envelope=envelope,
            ctx=ctx,
            receipt_id=receipt_id,
            decision=Decision.PERMIT,
            reason="graduated to autonomous per RACI",
            matched_rules=(),
        )

    # Step 4: novel-action default — propose to human.
    return _emit(
        envelope=envelope,
        ctx=ctx,
        receipt_id=receipt_id,
        decision=Decision.PROPOSE_TO_HUMAN,
        reason="novel action class; no graduation yet",
        matched_rules=(),
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _new_receipt_id() -> str:
    return f"authz-{uuid.uuid4().hex}"


def _disposition_to_decision(disposition: str) -> Decision:
    return {
        "permit": Decision.PERMIT,
        "deny": Decision.DENY,
        "propose": Decision.PROPOSE_TO_HUMAN,
        # require_capability is conceptually "deny until you re-present a
        # better capability" — for v1 we surface as DENY with a specific
        # reason.
        "require_capability": Decision.DENY,
    }[disposition]


def _is_graduated(envelope: ActionEnvelope, ctx: DecideContext) -> bool:
    """Has this (actor, intent_class) graduated to autonomous?"""
    if ctx.session_factory is None:
        return False
    try:
        with ctx.session_factory() as session:  # type: ignore[misc]
            row = _find_graduation(
                session,
                actor=envelope.actor.handle,
                intent_class=envelope.intent.primitive,
            )
            return bool(row and row.graduated)
    except Exception:
        # If the graduation lookup fails, conservatively DON'T graduate.
        # The novel-action propose path is the safe default.
        return False


def _find_graduation(
    session: Session, *, actor: str, intent_class: str
) -> Graduation | None:
    from sqlalchemy import select

    stmt = (
        select(Graduation)
        .where(Graduation.actor == actor)
        .where(Graduation.intent_class == intent_class)
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def _emit(
    *,
    envelope: ActionEnvelope,
    ctx: DecideContext,
    receipt_id: str,
    decision: Decision,
    reason: str,
    matched_rules: tuple[str, ...],
) -> Verdict:
    """Write the receipt fragment AND return the Verdict."""
    _write_receipt(
        ctx=ctx,
        receipt_id=receipt_id,
        envelope=envelope,
        decision=decision,
        reason=reason,
        matched_rules=matched_rules,
    )
    return Verdict.from_decision(decision, reason, receipt_id)


def _write_receipt(
    *,
    ctx: DecideContext,
    receipt_id: str,
    envelope: ActionEnvelope,
    decision: Decision,
    reason: str,
    matched_rules: tuple[str, ...],
) -> None:
    """Persist the receipt. Silent best-effort if session is unavailable."""
    if ctx.session_factory is None:
        return
    try:
        with ctx.session_factory() as session:  # type: ignore[misc]
            row = VerdictRow(
                id=receipt_id,
                actor=envelope.actor.handle,
                intent=envelope.intent.value,
                resource=str(envelope.resource),
                classification=envelope.classification.value,
                capability_id=envelope.capability.id,
                context_fragment_id=envelope.context_fragment_id,
                provenance_parent=str(envelope.provenance_parent),
                federation_origin=envelope.federation_origin,
                dedup_key=envelope.dedup_key,
                decision=decision.value,
                reason=reason,
                matched_rules=list(matched_rules) if matched_rules else None,
            )
            session.add(row)
            session.commit()
    except Exception:
        # Receipt write failure is loud — but we don't fail the action
        # itself. A separate hygiene check (TIDY) audits receipt-write
        # rates; persistent failure is a hygiene finding.
        pass


# ---------------------------------------------------------------------------
# Backwards-compat: the simplest decide() interface for callers that
# don't yet own a DecideContext. They get permit-all (safe in dev only).
# ---------------------------------------------------------------------------

_DEFAULT_CTX = DecideContext()


def decide_with_default_context(envelope: ActionEnvelope) -> Verdict:
    """Module-level decide() using a permit-all default ctx.

    For early-migration call sites + tests. Real consumers construct
    their own ``DecideContext`` with a session factory + curated rules.
    """
    return decide(envelope, _DEFAULT_CTX)


__all__ = [
    "DecideContext",
    "decide",
    "decide_with_default_context",
]
