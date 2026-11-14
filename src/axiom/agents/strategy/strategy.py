# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ModelStrategy Protocol + BaseStrategyResolver — spec-model-routing §13.

Resolution rules (in order, per §13.3):

1. Classification clamp — drop providers whose tier crosses the input.
2. Network reachability — drop providers whose tier isn't reachable.
3. Hard cohort constraints — apply forbidden_providers + required_provider_class.
4. Budget filter — drop providers whose estimated cost exceeds remaining budget.
5. User preference + role hint — order by user prefer; fall back to role hints.
6. Federation fallback — consult federation_peers if no local survives.
7. Health degradation — skip down providers; record in audit.

A role with no surviving provider raises ``ModelStrategyUnsatisfiable``.
The caller (PlanPipeline / AgentPipeline) decides whether to abort, prompt,
or relax policy.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from axiom.agents.strategy.types import (
    CohortModelPolicy,
    ModelContext,
    ModelRole,
    ProviderChoice,
    ProviderHealth,
    ProviderSpec,
    ResolvedAssembly,
    UserModelPolicy,
)
from axiom.vega.federation.policy import ClassificationStamp

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ModelStrategyUnsatisfiable(Exception):
    """Raised when a role cannot be resolved to any provider given the context."""

    def __init__(self, role: ModelRole, reasons: Sequence[str]):
        self.role = role
        self.reasons = tuple(reasons)
        super().__init__(
            f"role {role.value!r} unsatisfiable: " + "; ".join(reasons)
        )


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ModelStrategy(Protocol):
    name: str
    def resolve(self, role: ModelRole, ctx: ModelContext) -> ProviderChoice: ...
    def resolve_assembly(
        self, roles: Sequence[ModelRole], ctx: ModelContext
    ) -> ResolvedAssembly: ...


# ---------------------------------------------------------------------------
# Base resolver — implements §13.3 rules
# ---------------------------------------------------------------------------


def _is_classification_compliant(
    provider: ProviderSpec,
    classification: ClassificationStamp,
    cohort_cui_providers: tuple[str, ...],
) -> bool:
    level = getattr(classification, "level", "unclassified")
    if level == "unclassified":
        return True
    # CUI / classified: only providers explicitly whitelisted as cui_providers
    # OR providers in the private tier (assumed to satisfy unless cohort restricts).
    if cohort_cui_providers:
        return provider.name in cohort_cui_providers
    return provider.tier == "private"


def _is_network_reachable(
    provider: ProviderSpec, network_reachable: frozenset[str]
) -> bool:
    if provider.tier == "any":
        return bool(network_reachable)
    if provider.tier == "public":
        return "public" in network_reachable
    if provider.tier == "private":
        return "private" in network_reachable
    return False


def _passes_cohort_constraints(
    provider: ProviderSpec, cohort: CohortModelPolicy
) -> bool:
    if provider.name in cohort.forbidden_providers:
        return False
    if cohort.required_provider_class == "private-only":
        if provider.tier != "private":
            return False
    return True


def _passes_user_forbid(provider: ProviderSpec, user: UserModelPolicy) -> bool:
    return provider.name not in user.forbid


def _is_healthy(
    provider: ProviderSpec, health_map: Mapping[str, ProviderHealth]
) -> bool:
    health = health_map.get(provider.name)
    if health is None:
        return True   # unknown health: assume up
    return health.up


def _estimated_cost(provider: ProviderSpec, role: ModelRole) -> float:
    """Estimate cost for a single 1k-token-equivalent role invocation."""
    return provider.estimated_cost_per_1k_tokens_usd


def _passes_budget(
    provider: ProviderSpec,
    role: ModelRole,
    remaining_budget: float,
    n_remaining_roles: int,
) -> bool:
    cost = _estimated_cost(provider, role)
    if remaining_budget <= 0:
        return cost == 0
    per_role = remaining_budget / max(1, n_remaining_roles)
    return cost <= per_role


def _user_prefer_index(provider: ProviderSpec, prefer: tuple[str, ...]) -> int:
    try:
        return prefer.index(provider.name)
    except ValueError:
        return len(prefer)   # not in prefer list: lowest priority


def _role_hint_match(provider: ProviderSpec, role: ModelRole) -> int:
    if not provider.role_hints:
        return 1   # no hints: neutral
    return 0 if role in provider.role_hints else 2


@dataclass
class BaseStrategyResolver:
    """Reference implementation of the §13.3 resolution rules.

    Concrete strategies (legacy-router, cost-conservative, etc.) wrap or
    subclass this with strategy-specific defaults / preferences.
    """

    name: str
    providers: tuple[ProviderSpec, ...] = field(default_factory=tuple)

    def resolve(self, role: ModelRole, ctx: ModelContext) -> ProviderChoice:
        return self._resolve_role(role, ctx, n_remaining_roles=1)

    def _resolve_role(
        self, role: ModelRole, ctx: ModelContext, n_remaining_roles: int
    ) -> ProviderChoice:
        reasons: list[str] = []
        survivors: list[ProviderSpec] = []

        for p in self.providers:
            # Step 1 — classification clamp
            if not _is_classification_compliant(
                p, ctx.classification, ctx.cohort_policy.cui_providers
            ):
                reasons.append(
                    f"{p.name}: classification clamp ({ctx.classification.level if hasattr(ctx.classification, 'level') else ctx.classification})"
                )
                continue
            # Step 2 — reachability
            if not _is_network_reachable(p, ctx.network_reachable):
                reasons.append(
                    f"{p.name}: tier {p.tier} not in reachable {sorted(ctx.network_reachable)}"
                )
                continue
            # Step 3 — cohort hard constraints
            if not _passes_cohort_constraints(p, ctx.cohort_policy):
                reasons.append(f"{p.name}: cohort policy excludes")
                continue
            # User forbid
            if not _passes_user_forbid(p, ctx.user_policy):
                reasons.append(f"{p.name}: user forbids")
                continue
            # Step 4 — budget
            if not _passes_budget(
                p, role, ctx.budget_remaining_usd, n_remaining_roles
            ):
                reasons.append(
                    f"{p.name}: cost {p.estimated_cost_per_1k_tokens_usd:.4f} exceeds per-role budget"
                )
                continue
            # Step 7 — health
            if not _is_healthy(p, ctx.available_providers):
                reasons.append(f"{p.name}: provider down")
                continue
            survivors.append(p)

        if not survivors:
            raise ModelStrategyUnsatisfiable(role, reasons)

        # Step 5 — user preference + role hint
        survivors.sort(
            key=lambda p: (
                _user_prefer_index(p, ctx.user_policy.prefer),
                _role_hint_match(p, role),
                p.estimated_cost_per_1k_tokens_usd,
            )
        )

        chosen = survivors[0]
        return ProviderChoice(
            provider=chosen.name,
            model=chosen.model,
            tier=chosen.tier,
            estimated_cost_usd=_estimated_cost(chosen, role),
            via="local",
        )

    def resolve_assembly(
        self, roles: Sequence[ModelRole], ctx: ModelContext
    ) -> ResolvedAssembly:
        by_role: dict[ModelRole, ProviderChoice] = {}
        rationale_parts: list[str] = [f"strategy={self.name}"]
        total_cost = 0.0

        for i, role in enumerate(roles):
            n_remaining = len(roles) - i
            choice = self._resolve_role(role, ctx, n_remaining_roles=n_remaining)
            by_role[role] = choice
            rationale_parts.append(f"{role.value}={choice.provider}")
            total_cost += choice.estimated_cost_usd

        rationale = "; ".join(rationale_parts)

        return ResolvedAssembly(
            by_role=by_role,
            rationale=rationale,
            estimated_cost_usd=total_cost,
            classification_clamp=ctx.classification,
        )
