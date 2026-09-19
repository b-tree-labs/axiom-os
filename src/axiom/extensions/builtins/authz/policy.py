# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The policy-source registry and combiner (ADR-083).

decide()'s deterministic capability floor stays hard-coded and first — it is the
constitution. *Above* the floor, the policy layer used to be a fixed sequence
(substrate → rules → graduation → default). This module turns that sequence into
a registry of named :class:`PolicySource` s combined by one explicit algorithm,
so a new source (OpenFGA, a rate limiter, a break-glass override) is a
registration, not an edit to ``decide()``.

**Combining algorithm** — deny-overrides, then priority, then propose:

1. **Deny-overrides.** If *any* source denies, the result is DENY — even a
   low-priority denier beats a high-priority permit. Safety is not a contest.
2. **Highest-priority positive.** With no denial, the highest-priority
   non-abstaining opinion wins. This is what makes a substrate ALLOW
   *authoritative*: a lone relationship grant short-circuits the propose default
   (the P2 change over P1's fall-through), while a curated rule still outranks it.
3. **Propose fallback.** All sources abstain → novel action → propose to a human.

Sources that cannot deny (``may_deny=False``, e.g. a graduation DB lookup) are
evaluated lazily — skipped once a higher-priority source has decided.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from axiom.extensions.builtins.authz.substrate import SubstrateDecision

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.orm import Session

    from axiom.extensions.builtins.authz.decide import DecideContext
    from axiom.governance import ActionEnvelope


class PolicyEffect(Enum):
    """A source's opinion on an action."""

    PERMIT = "permit"
    DENY = "deny"
    PROPOSE = "propose"
    ABSTAIN = "abstain"


@dataclass(frozen=True)
class PolicyOpinion:
    """One source's opinion, with the reason and any rules that produced it."""

    effect: PolicyEffect
    reason: str = ""
    matched_rules: tuple[str, ...] = ()


#: The shared "no opinion" sentinel (sources return this to stay out of the way).
ABSTAIN = PolicyOpinion(PolicyEffect.ABSTAIN)


@dataclass(frozen=True)
class CombinedDecision:
    """The combiner's output: the winning effect, why, and which source won."""

    effect: PolicyEffect
    reason: str
    source: str
    matched_rules: tuple[str, ...] = ()


@runtime_checkable
class PolicySource(Protocol):
    """A named, prioritised source of authorization opinions.

    ``priority`` orders positive resolution (higher wins). ``may_deny`` declares
    whether the source can ever return DENY; a ``False`` source is evaluated
    lazily, since it can only add a permit/propose/abstain.
    """

    name: str
    priority: int
    may_deny: bool

    def evaluate(
        self, envelope: ActionEnvelope, ctx: DecideContext, now: datetime
    ) -> PolicyOpinion: ...


#: Novel-action fallback reason (kept stable — it lands in the audit receipt).
_DEFAULT_REASON = "novel action class; no graduation yet"


class PolicySourceRegistry:
    """An ordered set of policy sources with the deny-overrides combiner."""

    def __init__(self, sources: tuple[PolicySource, ...] | list[PolicySource] = ()) -> None:
        self._sources: list[PolicySource] = self._ordered(list(sources))

    @staticmethod
    def _ordered(sources: list[PolicySource]) -> list[PolicySource]:
        # Stable sort by descending priority: insertion order breaks ties.
        return sorted(sources, key=lambda s: -s.priority)

    def register(self, source: PolicySource) -> None:
        self._sources = self._ordered([*self._sources, source])

    @property
    def sources(self) -> tuple[PolicySource, ...]:
        return tuple(self._sources)

    def combine(
        self,
        *,
        envelope: ActionEnvelope,
        ctx: DecideContext,
        now: datetime,
    ) -> CombinedDecision:
        opinions: dict[str, PolicyOpinion] = {}

        # Phase 1 — deny-overrides. Every may_deny source is consulted; the
        # highest-priority denier (first, since sorted) wins immediately.
        for src in self._sources:
            if not src.may_deny:
                continue
            opinion = src.evaluate(envelope, ctx, now)
            opinions[src.name] = opinion
            if opinion.effect is PolicyEffect.DENY:
                return CombinedDecision(
                    PolicyEffect.DENY, opinion.reason, src.name, opinion.matched_rules
                )

        # Phase 2 — highest-priority positive opinion (lazy for non-deny sources).
        for src in self._sources:
            opinion = opinions.get(src.name)
            if opinion is None:
                opinion = src.evaluate(envelope, ctx, now)
            if opinion.effect is not PolicyEffect.ABSTAIN:
                return CombinedDecision(
                    opinion.effect, opinion.reason, src.name, opinion.matched_rules
                )

        # Phase 3 — everyone abstained: propose to a human.
        return CombinedDecision(PolicyEffect.PROPOSE, _DEFAULT_REASON, "default")


# ---------------------------------------------------------------------------
# Concrete sources — thin adapters over the existing decide() dependencies.
# ---------------------------------------------------------------------------

_DISPOSITION_EFFECT = {
    "permit": PolicyEffect.PERMIT,
    "deny": PolicyEffect.DENY,
    "propose": PolicyEffect.PROPOSE,
    # "deny until you re-present a stronger capability" — surfaced as DENY for v1.
    "require_capability": PolicyEffect.DENY,
}


@dataclass
class RuleEngineSource:
    """Explicit, curated per-resource/intent rules — the highest policy authority."""

    name: str = "rules"
    priority: int = 100
    may_deny: bool = True

    def evaluate(
        self, envelope: ActionEnvelope, ctx: DecideContext, now: datetime
    ) -> PolicyOpinion:
        combined = ctx.rule_engine.evaluate(envelope, now)
        if combined.disposition is None:
            return ABSTAIN
        return PolicyOpinion(
            _DISPOSITION_EFFECT[combined.disposition],
            f"matched rules: {', '.join(combined.matched_rules)}",
            combined.matched_rules,
        )


@dataclass
class SubstrateSource:
    """The fine-grained authorization substrate (OpenFGA, ADR-083).

    DENY overrides; ALLOW is authoritative (short-circuits propose) but still
    yields to a curated rule and to any denial.
    """

    name: str = "substrate"
    priority: int = 50
    may_deny: bool = True

    def evaluate(
        self, envelope: ActionEnvelope, ctx: DecideContext, now: datetime
    ) -> PolicyOpinion:
        decision = ctx.substrate.check(envelope)
        if decision is SubstrateDecision.DENY:
            return PolicyOpinion(PolicyEffect.DENY, "authorization substrate denied")
        if decision is SubstrateDecision.ALLOW:
            return PolicyOpinion(PolicyEffect.PERMIT, "authorization substrate allowed")
        return ABSTAIN


@dataclass
class GraduationSource:
    """Learned autonomy: an (actor, intent-class) that has graduated to PERMIT.

    Cannot deny — only elevates a would-be propose to permit — so it is evaluated
    lazily, after the rule engine and substrate.
    """

    name: str = "graduation"
    priority: int = 10
    may_deny: bool = False

    def evaluate(
        self, envelope: ActionEnvelope, ctx: DecideContext, now: datetime
    ) -> PolicyOpinion:
        if _is_graduated(envelope, ctx):
            return PolicyOpinion(PolicyEffect.PERMIT, "graduated to autonomous per RACI")
        return ABSTAIN


def _is_graduated(envelope: ActionEnvelope, ctx: DecideContext) -> bool:
    """Has this (actor, intent_class) graduated to autonomous? Fail-closed."""
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
        # A failed lookup must not grant autonomy — the propose path is safe.
        return False


def _find_graduation(session: Session, *, actor: str, intent_class: str):
    from sqlalchemy import select

    from axiom.extensions.builtins.authz.db_models import Graduation

    stmt = (
        select(Graduation)
        .where(Graduation.actor == actor)
        .where(Graduation.intent_class == intent_class)
        .limit(1)
    )
    return session.execute(stmt).scalar_one_or_none()


def default_policy_registry() -> PolicySourceRegistry:
    """The standard GUARD policy stack: rules > substrate > graduation."""
    return PolicySourceRegistry(
        [RuleEngineSource(), SubstrateSource(), GraduationSource()]
    )


__all__ = [
    "ABSTAIN",
    "CombinedDecision",
    "GraduationSource",
    "PolicyEffect",
    "PolicyOpinion",
    "PolicySource",
    "PolicySourceRegistry",
    "RuleEngineSource",
    "SubstrateSource",
    "default_policy_registry",
]
