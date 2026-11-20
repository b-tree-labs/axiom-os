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
   provenance-stamped audit fragment via `session_for('authz')`. No
   receipt, no action: a decision whose receipt can't be written is DENY.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from axiom.extensions.builtins.authz.db_models import (
    Verdict as VerdictRow,
)
from axiom.extensions.builtins.authz.policy import (
    PolicyEffect,
    PolicySourceRegistry,
    default_policy_registry,
)
from axiom.extensions.builtins.authz.rules import (
    Rule,
    RuleEngine,
)
from axiom.extensions.builtins.authz.substrate import (
    AuthzSubstrate,
    NullSubstrate,
)
from axiom.governance import (
    ActionEnvelope,
    Decision,
    Verdict,
)
from axiom.governance.mode import is_dev

_LOG = logging.getLogger("axiom.authz.decide")

#: The receipt id on the DENY returned when the receipt can't be written. It
#: names no row, so no receipt lookup resolves it.
UNRECORDED_RECEIPT = "unrecorded"
RECEIPT_WRITE_FAILED = "receipt write failed"
#: Prefix of the ids the dev-only no-op sink hands out (no ``session_factory``,
#: or no usable receipt table, in dev mode). Still unique per call: approval
#: tokens key off the id.
_DEV_UNRECORDED_PREFIX = "authz-unrecorded-"


class ReceiptWriteError(RuntimeError):
    """The receipt for a decision could not be written. The action must not run."""

#: PolicyEffect → the caller-facing Decision the combiner's outcome maps to.
_EFFECT_TO_DECISION = {
    PolicyEffect.PERMIT: Decision.PERMIT,
    PolicyEffect.DENY: Decision.DENY,
    PolicyEffect.PROPOSE: Decision.PROPOSE_TO_HUMAN,
}


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
    ``None`` is the no-op sink, allowed only in dev mode (``AXIOM_MODE``):
    the verdict's receipt id starts ``authz-unrecorded-``. In staging and
    production ``None`` makes every decision DENY.
    """
    receipt_table_usable: Callable[[], bool] | None = None
    """Asked once a receipt write has failed, in dev mode only.

    Contexts are cached for the life of a process, so the database a dev process
    started against may not be the one it writes to now. ``False`` sends this
    decision to the no-op sink; ``True``, or no answer at all, leaves it a
    failed write. Ignored in staging and production, where a failed write
    always denies. Only contexts wired to the platform database set it.
    """
    default_threshold: int = 5
    substrate: AuthzSubstrate = field(default_factory=NullSubstrate)
    """The fine-grained authorization substrate GUARD consults (ADR-083).

    Default ``NullSubstrate`` abstains on everything, so the decision pipeline is
    behaviour-preserving until a real backend (``OpenFgaSubstrate``, P2) is
    registered. Deployments that mandate substrate coverage register
    ``DenyAllSubstrate`` instead so un-modelled actions deny rather than abstain.
    """
    policy_sources: PolicySourceRegistry = field(default_factory=default_policy_registry)
    """The policy layer above the capability floor (ADR-083).

    The registry of :class:`PolicySource` s (rules, substrate, graduation) that
    ``decide()`` combines with deny-overrides. Swap or extend it to register a new
    source (a rate limiter, break-glass, a second substrate) without touching
    ``decide()``.
    """

    def add_rule(self, rule: Rule) -> None:
        self.rule_engine.add(rule)


def decide(envelope: ActionEnvelope, ctx: DecideContext) -> Verdict:
    """The single decision API. Returns a typed Verdict.

    The decision pipeline:

    1. Capability lifecycle + scope check — expired / out-of-scope token →
       ``EXPIRED_CAPABILITY`` / ``DENY``. The deterministic floor, first and
       non-negotiable; it is never a registered source.
    2. The policy layer — ``ctx.policy_sources`` (rules > substrate > graduation)
       combined with deny-overrides, then highest-priority positive, then a
       propose fallback. See :mod:`.policy`.

    Every path writes a receipt. If it can't, the verdict is DENY with
    reason ``receipt write failed`` and receipt id ``UNRECORDED_RECEIPT``.
    """
    now = datetime.now(UTC)

    # Step 1: capability lifecycle.
    if not envelope.capability.is_valid_at(now):
        return _emit(
            envelope=envelope,
            ctx=ctx,
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
            decision=Decision.DENY,
            reason="capability does not permit this intent",
            matched_rules=(),
        )
    if not envelope.capability.permits_resource(envelope.resource):
        return _emit(
            envelope=envelope,
            ctx=ctx,
            decision=Decision.DENY,
            reason="capability does not permit this resource",
            matched_rules=(),
        )
    if not envelope.capability.permits_classification(envelope.classification):
        return _emit(
            envelope=envelope,
            ctx=ctx,
            decision=Decision.DENY,
            reason="capability classification ceiling exceeded",
            matched_rules=(),
        )

    # Step 2: the policy layer — registered sources combined with deny-overrides.
    combined = ctx.policy_sources.combine(envelope=envelope, ctx=ctx, now=now)
    return _emit(
        envelope=envelope,
        ctx=ctx,
        decision=_EFFECT_TO_DECISION[combined.effect],
        reason=combined.reason,
        matched_rules=combined.matched_rules,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _new_receipt_id() -> str:
    return f"authz-{uuid.uuid4().hex}"


def _emit(
    *,
    envelope: ActionEnvelope,
    ctx: DecideContext,
    decision: Decision,
    reason: str,
    matched_rules: tuple[str, ...],
) -> Verdict:
    """Write the receipt fragment AND return the Verdict.

    The receipt id is only set on a verdict whose row committed (or, in dev,
    went to the no-op sink). A failed write is DENY, never re-decided.
    """
    try:
        receipt_id = _write_receipt(
            ctx=ctx,
            envelope=envelope,
            decision=decision,
            reason=reason,
            matched_rules=matched_rules,
        )
    except ReceiptWriteError as exc:
        _LOG.error("denying: %s", exc)
        return Verdict.from_decision(Decision.DENY, RECEIPT_WRITE_FAILED, UNRECORDED_RECEIPT)
    return Verdict.from_decision(decision, reason, receipt_id)


def _write_receipt(
    *,
    ctx: DecideContext,
    envelope: ActionEnvelope,
    decision: Decision,
    reason: str,
    matched_rules: tuple[str, ...],
) -> str:
    """Persist the receipt and return its id; raise ``ReceiptWriteError`` if it can't.

    The error names intent and resource only — never envelope content, which
    a driver error's bound parameters would carry.
    """
    what = f"{envelope.intent.value} on {envelope.resource}"
    if ctx.session_factory is None:
        if is_dev():
            return f"{_DEV_UNRECORDED_PREFIX}{uuid.uuid4().hex}"
        raise ReceiptWriteError(f"receipt for {what} not written: no session factory outside dev mode")
    receipt_id = _new_receipt_id()
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
    except Exception as exc:  # noqa: BLE001 — any failure means no receipt
        if is_dev() and ctx.receipt_table_usable is not None and not ctx.receipt_table_usable():
            return f"{_DEV_UNRECORDED_PREFIX}{uuid.uuid4().hex}"
        raise ReceiptWriteError(f"receipt for {what} not written: {type(exc).__name__}") from exc
    return receipt_id


# ---------------------------------------------------------------------------
# Backwards-compat: the simplest decide() interface for callers that
# don't yet own a DecideContext. Dev only: it has no receipt sink, so
# outside dev mode every decision it makes is DENY.
# ---------------------------------------------------------------------------

_DEFAULT_CTX = DecideContext()


def decide_with_default_context(envelope: ActionEnvelope) -> Verdict:
    """Module-level decide() using a permit-all default ctx.

    For early-migration call sites + tests, in dev mode only (no session
    factory: see ``DecideContext.session_factory``). Real consumers construct
    their own ``DecideContext`` with a session factory + curated rules.
    """
    return decide(envelope, _DEFAULT_CTX)


__all__ = [
    "RECEIPT_WRITE_FAILED",
    "UNRECORDED_RECEIPT",
    "DecideContext",
    "ReceiptWriteError",
    "decide",
    "decide_with_default_context",
]
