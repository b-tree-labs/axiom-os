# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.agents.strategy.strategy — Protocol + BaseResolver.

Per spec-model-routing §13.3 resolution rules:
1. Classification clamp.
2. Network reachability filter.
3. Hard cohort constraints.
4. Budget filter.
5. User preference + role hint.
6. Federation fallback (optional).
7. Health degradation.
"""

from __future__ import annotations

import pytest

from axiom.agents.strategy.strategy import (
    BaseStrategyResolver,
    ModelStrategy,
    ModelStrategyUnsatisfiable,
)
from axiom.agents.strategy.types import (
    CohortModelPolicy,
    ModelContext,
    ModelRole,
    ProviderHealth,
    ProviderSpec,
    UserModelPolicy,
)
from axiom.vega.federation.policy import ClassificationStamp


def _public_provider(name: str, **kw) -> ProviderSpec:
    return ProviderSpec(
        name=name,
        tier="public",
        model=kw.get("model", f"{name}-model"),
        estimated_cost_per_1k_tokens_usd=kw.get("cost", 0.005),
        role_hints=kw.get("roles", ()),
    )


def _private_provider(name: str, **kw) -> ProviderSpec:
    return ProviderSpec(
        name=name,
        tier="private",
        model=kw.get("model", f"{name}-model"),
        estimated_cost_per_1k_tokens_usd=kw.get("cost", 0.0),
        role_hints=kw.get("roles", ()),
        requires_vpn=kw.get("requires_vpn", True),
    )


def _ctx(
    *,
    classification: ClassificationStamp = None,
    budget: float = 100.0,
    reachable: frozenset[str] = None,
    user_prefer: tuple[str, ...] = (),
    user_forbid: tuple[str, ...] = (),
    cohort_forbid: tuple[str, ...] = (),
    cohort_required: str = None,
    cui_providers: tuple[str, ...] = (),
    health: dict = None,
) -> ModelContext:
    return ModelContext(
        classification=classification if classification is not None
        else ClassificationStamp.unclassified(),
        budget_remaining_usd=budget,
        network_reachable=reachable if reachable is not None
        else frozenset({"public", "private"}),
        user_policy=UserModelPolicy(prefer=user_prefer, forbid=user_forbid),
        cohort_policy=CohortModelPolicy(
            forbidden_providers=cohort_forbid,
            required_provider_class=cohort_required,
            cui_providers=cui_providers,
        ),
        available_providers=health if health is not None else {},
    )


class TestProtocolConformance:
    def test_base_resolver_conforms_to_protocol(self):
        resolver = BaseStrategyResolver(name="test", providers=())
        assert isinstance(resolver, ModelStrategy)


class TestResolveSimpleRole:
    def test_picks_only_available_provider(self):
        resolver = BaseStrategyResolver(
            name="test",
            providers=(_public_provider("anthropic"),),
        )
        ctx = _ctx()
        choice = resolver.resolve(ModelRole.EXECUTOR, ctx)
        assert choice.provider == "anthropic"

    def test_user_prefer_drives_order(self):
        resolver = BaseStrategyResolver(
            name="test",
            providers=(
                _public_provider("anthropic"),
                _public_provider("openai"),
            ),
        )
        ctx = _ctx(user_prefer=("openai",))
        choice = resolver.resolve(ModelRole.EXECUTOR, ctx)
        assert choice.provider == "openai"

    def test_user_forbid_excludes(self):
        resolver = BaseStrategyResolver(
            name="test",
            providers=(
                _public_provider("anthropic"),
                _public_provider("openai"),
            ),
        )
        ctx = _ctx(user_forbid=("openai",), user_prefer=("openai", "anthropic"))
        choice = resolver.resolve(ModelRole.EXECUTOR, ctx)
        assert choice.provider == "anthropic"


class TestClassificationClamp:
    def test_cui_input_excludes_public_providers(self):
        cui = ClassificationStamp(level="cui")
        resolver = BaseStrategyResolver(
            name="test",
            providers=(
                _public_provider("anthropic"),
                _private_provider("private-qwen"),
            ),
        )
        ctx = _ctx(classification=cui, cui_providers=("private-qwen",))
        choice = resolver.resolve(ModelRole.EXECUTOR, ctx)
        assert choice.provider == "private-qwen"
        assert choice.tier == "private"

    def test_unclassified_does_not_clamp(self):
        resolver = BaseStrategyResolver(
            name="test",
            providers=(_public_provider("anthropic"),),
        )
        ctx = _ctx()  # unclassified default
        choice = resolver.resolve(ModelRole.EXECUTOR, ctx)
        assert choice.provider == "anthropic"

    def test_cui_with_no_compliant_provider_raises(self):
        cui = ClassificationStamp(level="cui")
        resolver = BaseStrategyResolver(
            name="test",
            providers=(_public_provider("anthropic"),),  # no private
        )
        ctx = _ctx(classification=cui, cui_providers=("private-qwen",))
        with pytest.raises(ModelStrategyUnsatisfiable) as exc_info:
            resolver.resolve(ModelRole.EXECUTOR, ctx)
        assert "classification" in str(exc_info.value).lower()


class TestNetworkReachability:
    def test_no_public_reachable_excludes_public_providers(self):
        resolver = BaseStrategyResolver(
            name="test",
            providers=(
                _public_provider("anthropic"),
                _private_provider("private-qwen"),
            ),
        )
        ctx = _ctx(reachable=frozenset({"private"}))
        choice = resolver.resolve(ModelRole.EXECUTOR, ctx)
        assert choice.provider == "private-qwen"

    def test_no_reachable_at_all_raises(self):
        resolver = BaseStrategyResolver(
            name="test",
            providers=(_public_provider("anthropic"),),
        )
        ctx = _ctx(reachable=frozenset())
        with pytest.raises(ModelStrategyUnsatisfiable):
            resolver.resolve(ModelRole.EXECUTOR, ctx)


class TestCohortHardConstraints:
    def test_cohort_forbidden_overrides_user_prefer(self):
        resolver = BaseStrategyResolver(
            name="test",
            providers=(
                _public_provider("openai-gpt4-turbo"),
                _public_provider("anthropic"),
            ),
        )
        ctx = _ctx(
            user_prefer=("openai-gpt4-turbo",),
            cohort_forbid=("openai-gpt4-turbo",),
        )
        choice = resolver.resolve(ModelRole.EXECUTOR, ctx)
        assert choice.provider == "anthropic"

    def test_cohort_required_class_filters(self):
        resolver = BaseStrategyResolver(
            name="test",
            providers=(
                _public_provider("anthropic"),
                _private_provider("private-qwen"),
            ),
        )
        ctx = _ctx(cohort_required="private-only")
        choice = resolver.resolve(ModelRole.EXECUTOR, ctx)
        assert choice.tier == "private"


class TestBudget:
    def test_budget_filter_excludes_too_expensive(self):
        resolver = BaseStrategyResolver(
            name="test",
            providers=(
                _public_provider("expensive", cost=10.0),
                _public_provider("cheap", cost=0.001),
            ),
        )
        # Budget too small for "expensive" estimate (cost-per-1k * 1k tokens)
        ctx = _ctx(budget=0.5)
        choice = resolver.resolve(ModelRole.EXECUTOR, ctx)
        assert choice.provider == "cheap"


class TestProviderHealth:
    def test_down_provider_skipped(self):
        resolver = BaseStrategyResolver(
            name="test",
            providers=(
                _public_provider("anthropic"),
                _public_provider("openai"),
            ),
        )
        ctx = _ctx(
            user_prefer=("anthropic",),
            health={"anthropic": ProviderHealth(up=False)},
        )
        choice = resolver.resolve(ModelRole.EXECUTOR, ctx)
        assert choice.provider == "openai"


class TestResolveAssembly:
    def test_assembly_resolves_each_role(self):
        resolver = BaseStrategyResolver(
            name="test",
            providers=(
                _public_provider("router-cheap", cost=0.0001, roles=(ModelRole.ROUTER,)),
                _public_provider("anthropic", cost=0.005, roles=(ModelRole.EXECUTOR,)),
            ),
        )
        ctx = _ctx()
        assembly = resolver.resolve_assembly(
            (ModelRole.ROUTER, ModelRole.EXECUTOR), ctx
        )
        assert assembly.by_role[ModelRole.ROUTER].provider == "router-cheap"
        assert assembly.by_role[ModelRole.EXECUTOR].provider == "anthropic"
        assert assembly.estimated_cost_usd > 0
        assert "rationale" in assembly.to_audit_payload() or assembly.rationale

    def test_assembly_rationale_non_empty(self):
        resolver = BaseStrategyResolver(
            name="test",
            providers=(_public_provider("anthropic"),),
        )
        ctx = _ctx()
        assembly = resolver.resolve_assembly((ModelRole.EXECUTOR,), ctx)
        assert assembly.rationale  # non-empty

    def test_assembly_unsatisfiable_role_raises(self):
        cui = ClassificationStamp(level="cui")
        resolver = BaseStrategyResolver(
            name="test",
            providers=(_public_provider("anthropic"),),
        )
        ctx = _ctx(classification=cui, cui_providers=("private-qwen",))
        with pytest.raises(ModelStrategyUnsatisfiable):
            resolver.resolve_assembly((ModelRole.EXECUTOR,), ctx)
