# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Rule + RuleEngine — declarative authorization rules over `ActionEnvelope`.

Per prd-axiom-authz §5.2. The engine evaluates all matching rules and
combines per documented precedence:

1. ``deny`` wins any tie.
2. Explicit ``propose`` beats implicit ``permit``.
3. Higher ``priority`` wins per-disposition.
4. Empty match set → caller's caller (default disposition, typically
   ``propose_to_human`` from the graduation layer).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from axiom.governance import (
    ActionEnvelope,
    Classification,
    IntentPattern,
    ResourcePattern,
)

Disposition = Literal["permit", "deny", "propose", "require_capability"]


@dataclass(frozen=True)
class Rule:
    """A declarative match → disposition rule.

    Field semantics match spec-governance-fabric + prd-axiom-authz §5.2.
    """

    name: str
    intent_pattern: IntentPattern
    actor_pattern: str
    """Currently a literal-match-or-``*``; PrincipalPattern is a follow-up."""
    resource_pattern: ResourcePattern
    classification: frozenset[Classification] = field(
        default_factory=lambda: frozenset(Classification)
    )
    """The set of classifications this rule applies to; default: all."""
    federation_origin_pattern: str | None = None
    """``None`` matches local-only; a string matches inbound peer id."""
    disposition: Disposition = "permit"
    priority: int = 0
    ttl: datetime | None = None

    def matches(self, env: ActionEnvelope, now: datetime | None = None) -> bool:
        """Return True iff this rule applies to ``env`` at ``now``."""
        if self.ttl is not None:
            now = now or datetime.now(timezone.utc)
            if now > self.ttl:
                return False
        if not self.intent_pattern.matches(env.intent):
            return False
        if self.actor_pattern != "*" and self.actor_pattern != env.actor.handle:
            return False
        if not self.resource_pattern.matches(env.resource):
            return False
        if env.classification not in self.classification:
            return False
        if self.federation_origin_pattern is None:
            # Rule applies only to locally-originated actions.
            if env.federation_origin is not None:
                return False
        else:
            if env.federation_origin != self.federation_origin_pattern:
                return False
        return True


@dataclass(frozen=True)
class CombinedDisposition:
    """The outcome of evaluating a rule set against an envelope."""

    disposition: Disposition | None
    """``None`` means no rule matched."""
    matched_rules: tuple[str, ...]
    """Names of rules that matched, in evaluation order."""


@dataclass
class RuleEngine:
    """Evaluates rules against envelopes per the documented precedence."""

    rules: list[Rule] = field(default_factory=list)

    def add(self, rule: Rule) -> None:
        self.rules.append(rule)

    def evaluate(
        self, env: ActionEnvelope, now: datetime | None = None
    ) -> CombinedDisposition:
        matched = [r for r in self.rules if r.matches(env, now)]
        if not matched:
            return CombinedDisposition(disposition=None, matched_rules=())

        # Precedence: deny > propose > require_capability > permit.
        # Within a disposition, higher priority wins (but the disposition
        # itself is what's returned).
        deny = [r for r in matched if r.disposition == "deny"]
        propose = [r for r in matched if r.disposition == "propose"]
        require_cap = [
            r for r in matched if r.disposition == "require_capability"
        ]
        permit = [r for r in matched if r.disposition == "permit"]

        if deny:
            chosen = max(deny, key=lambda r: r.priority)
        elif propose:
            chosen = max(propose, key=lambda r: r.priority)
        elif require_cap:
            chosen = max(require_cap, key=lambda r: r.priority)
        elif permit:
            chosen = max(permit, key=lambda r: r.priority)
        else:
            # Unreachable; matched is non-empty.
            chosen = matched[0]

        return CombinedDisposition(
            disposition=chosen.disposition,
            matched_rules=tuple(r.name for r in matched),
        )


__all__ = ["CombinedDisposition", "Disposition", "Rule", "RuleEngine"]
