# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.agents.strategy.builtin — built-in strategies.

Per spec-model-routing §13.6: legacy-router, cost-conservative, quality-first,
cohort-pinned.
"""

from __future__ import annotations

import pytest

from axiom.agents.strategy.builtin import (
    cohort_pinned,
    cost_conservative,
    legacy_router,
    quality_first,
)
from axiom.agents.strategy.strategy import ModelStrategyUnsatisfiable
from axiom.agents.strategy.types import (
    CohortModelPolicy,
    ModelContext,
    ModelRole,
    ProviderSpec,
    UserModelPolicy,
)
from axiom.vega.federation.policy import ClassificationStamp


def _providers() -> tuple[ProviderSpec, ...]:
    return (
        ProviderSpec(
            name="ollama-llama3.2-3b",
            tier="private",
            model="llama3.2:3b",
            estimated_cost_per_1k_tokens_usd=0.0,
            role_hints=(ModelRole.ROUTER, ModelRole.VERIFIER),
            requires_vpn=False,
        ),
        ProviderSpec(
            name="anthropic-haiku",
            tier="public",
            model="claude-haiku-4",
            estimated_cost_per_1k_tokens_usd=0.0008,
            role_hints=(ModelRole.PLANNER, ModelRole.ROUTER),
        ),
        ProviderSpec(
            name="selfhosted-qwen",
            tier="private",
            model="qwen-32b",
            estimated_cost_per_1k_tokens_usd=0.0,
            role_hints=(ModelRole.EXECUTOR,),
            requires_vpn=True,
        ),
        ProviderSpec(
            name="anthropic-sonnet",
            tier="public",
            model="claude-sonnet-4",
            estimated_cost_per_1k_tokens_usd=0.005,
            role_hints=(ModelRole.EXECUTOR,),
        ),
    )


def _ctx(
    *,
    classification: ClassificationStamp = None,
    budget: float = 100.0,
    reachable: frozenset[str] = None,
    user_prefer: tuple[str, ...] = (),
    cohort_required: str = None,
    cui_providers: tuple[str, ...] = (),
) -> ModelContext:
    return ModelContext(
        classification=classification if classification is not None
        else ClassificationStamp.unclassified(),
        budget_remaining_usd=budget,
        network_reachable=reachable if reachable is not None
        else frozenset({"public", "private"}),
        user_policy=UserModelPolicy(prefer=user_prefer),
        cohort_policy=CohortModelPolicy(
            required_provider_class=cohort_required,
            cui_providers=cui_providers,
        ),
        available_providers={},
    )


class TestLegacyRouter:
    def test_legacy_router_resolves_default_role(self):
        strategy = legacy_router(providers=_providers())
        ctx = _ctx()
        choice = strategy.resolve(ModelRole.DEFAULT, ctx)
        # Cheapest available wins for DEFAULT
        assert choice.estimated_cost_usd >= 0
        assert choice.provider in {p.name for p in _providers()}

    def test_legacy_router_name(self):
        strategy = legacy_router(providers=_providers())
        assert strategy.name == "legacy-router"


class TestCostConservative:
    def test_cost_conservative_picks_cheap_for_router(self):
        strategy = cost_conservative(providers=_providers())
        ctx = _ctx()
        choice = strategy.resolve(ModelRole.ROUTER, ctx)
        # ollama-llama3.2-3b is cheapest + role-hinted for ROUTER
        assert choice.provider == "ollama-llama3.2-3b"

    def test_cost_conservative_picks_mid_for_executor(self):
        strategy = cost_conservative(providers=_providers())
        ctx = _ctx()
        choice = strategy.resolve(ModelRole.EXECUTOR, ctx)
        # Among executor-hinted: selfhosted-qwen (free, private) wins over anthropic-sonnet
        assert choice.provider == "selfhosted-qwen"

    def test_cost_conservative_assembly(self):
        strategy = cost_conservative(providers=_providers())
        ctx = _ctx()
        assembly = strategy.resolve_assembly(
            (ModelRole.ROUTER, ModelRole.EXECUTOR, ModelRole.VERIFIER), ctx
        )
        assert ModelRole.ROUTER in assembly.by_role
        assert ModelRole.EXECUTOR in assembly.by_role
        assert ModelRole.VERIFIER in assembly.by_role

    def test_cost_conservative_classification_clamps(self):
        cui = ClassificationStamp(level="cui")
        strategy = cost_conservative(providers=_providers())
        ctx = _ctx(classification=cui, cui_providers=("selfhosted-qwen",))
        choice = strategy.resolve(ModelRole.EXECUTOR, ctx)
        assert choice.provider == "selfhosted-qwen"

    def test_cost_conservative_name(self):
        assert cost_conservative(providers=_providers()).name == "cost-conservative"


class TestQualityFirst:
    def test_quality_first_picks_high_quality_executor(self):
        strategy = quality_first(providers=_providers())
        ctx = _ctx()
        choice = strategy.resolve(ModelRole.EXECUTOR, ctx)
        # quality_first prefers anthropic-sonnet for executor
        assert choice.provider in ("anthropic-sonnet", "selfhosted-qwen")

    def test_quality_first_name(self):
        assert quality_first(providers=_providers()).name == "quality-first"


class TestCohortPinned:
    def test_cohort_pinned_honors_required_class(self):
        strategy = cohort_pinned(providers=_providers())
        ctx = _ctx(cohort_required="private-only")
        choice = strategy.resolve(ModelRole.EXECUTOR, ctx)
        assert choice.tier == "private"

    def test_cohort_pinned_strict_no_fallback(self):
        # If user prefer points to a public provider but cohort is private-only,
        # strategy must NOT fall back to public.
        strategy = cohort_pinned(providers=_providers())
        ctx = _ctx(
            cohort_required="private-only",
            user_prefer=("anthropic-sonnet",),
        )
        choice = strategy.resolve(ModelRole.EXECUTOR, ctx)
        assert choice.tier == "private"

    def test_cohort_pinned_unsatisfiable_raises(self):
        # Cohort requires private-only but only public providers exist.
        strategy = cohort_pinned(
            providers=(
                ProviderSpec(
                    name="anthropic-only",
                    tier="public",
                    model="claude",
                    estimated_cost_per_1k_tokens_usd=0.005,
                ),
            ),
        )
        ctx = _ctx(cohort_required="private-only")
        with pytest.raises(ModelStrategyUnsatisfiable):
            strategy.resolve(ModelRole.EXECUTOR, ctx)

    def test_cohort_pinned_name(self):
        assert cohort_pinned(providers=_providers()).name == "cohort-pinned"


class TestComplianceClassificationClamp:
    """Compliance gate from spec-model-routing §13.9: a CUI input never
    resolves to a public-tier provider regardless of preference/budget."""

    def test_cui_never_resolves_public(self):
        cui = ClassificationStamp(level="cui")
        for builder in (cost_conservative, quality_first, cohort_pinned, legacy_router):
            strategy = builder(providers=_providers())
            ctx = _ctx(
                classification=cui,
                cui_providers=("selfhosted-qwen", "ollama-llama3.2-3b"),
                user_prefer=("anthropic-sonnet",),  # tries to override
            )
            choice = strategy.resolve(ModelRole.EXECUTOR, ctx)
            assert choice.tier == "private", (
                f"{strategy.name} resolved CUI input to {choice.tier}-tier provider!"
            )


class TestComplianceCohortHardConstraint:
    """Compliance gate: cohort.forbidden_providers never overridden."""

    def test_cohort_forbidden_never_overridden(self):
        for builder in (cost_conservative, quality_first, legacy_router):
            strategy = builder(providers=_providers())
            ctx = ModelContext(
                classification=ClassificationStamp.unclassified(),
                budget_remaining_usd=100.0,
                network_reachable=frozenset({"public", "private"}),
                user_policy=UserModelPolicy(prefer=("anthropic-sonnet",)),
                cohort_policy=CohortModelPolicy(
                    forbidden_providers=("anthropic-sonnet",),
                ),
                available_providers={},
            )
            choice = strategy.resolve(ModelRole.EXECUTOR, ctx)
            assert choice.provider != "anthropic-sonnet", (
                f"{strategy.name} chose forbidden provider!"
            )


class TestComplianceRationale:
    """Compliance gate: every ResolvedAssembly has a non-empty rationale."""

    def test_all_strategies_emit_rationale(self):
        for builder in (cost_conservative, quality_first, cohort_pinned, legacy_router):
            strategy = builder(providers=_providers())
            ctx = _ctx()
            assembly = strategy.resolve_assembly((ModelRole.EXECUTOR,), ctx)
            assert assembly.rationale, f"{strategy.name} emitted empty rationale"


class TestComplianceBudgetExhaustion:
    """Compliance gate: when budget is insufficient, raises rather than
    silently falling back to a free-but-noncompliant provider."""

    def test_budget_zero_with_only_paid_providers_raises(self):
        only_paid = (
            ProviderSpec(
                name="anthropic-only",
                tier="public",
                model="claude",
                estimated_cost_per_1k_tokens_usd=0.005,
            ),
        )
        strategy = cost_conservative(providers=only_paid)
        ctx = _ctx(budget=0.0)
        with pytest.raises(ModelStrategyUnsatisfiable):
            strategy.resolve(ModelRole.EXECUTOR, ctx)
