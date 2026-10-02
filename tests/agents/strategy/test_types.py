# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.agents.strategy.types — ModelStrategy primitive types."""

from __future__ import annotations

import pytest

from axiom.agents.strategy.types import (
    CohortModelPolicy,
    ModelContext,
    ModelRole,
    ProviderChoice,
    ProviderHealth,
    ResolvedAssembly,
    UserModelPolicy,
)
from axiom.vega.federation.policy import ClassificationStamp


def _classification() -> ClassificationStamp:
    return ClassificationStamp.unclassified()


class TestModelRole:
    def test_enum_values(self):
        for r in (
            ModelRole.ROUTER,
            ModelRole.PLANNER,
            ModelRole.EXECUTOR,
            ModelRole.VERIFIER,
            ModelRole.EMBED,
            ModelRole.RERANK,
            ModelRole.DEFAULT,
        ):
            assert isinstance(r.value, str)


class TestProviderChoice:
    def test_basic_choice(self):
        choice = ProviderChoice(
            provider="anthropic",
            model="claude-sonnet-4",
            tier="public",
            estimated_cost_usd=0.003,
        )
        assert choice.provider == "anthropic"
        assert choice.via == "local"      # default

    def test_choice_via_federation(self):
        choice = ProviderChoice(
            provider="private-qwen",
            model="qwen-32b",
            tier="private",
            estimated_cost_usd=0.0,
            via="federation:example-org",
        )
        assert choice.via == "federation:example-org"

    def test_choice_is_frozen(self):
        choice = ProviderChoice(
            provider="x", model="m", tier="public", estimated_cost_usd=0.0
        )
        with pytest.raises((AttributeError, TypeError)):
            choice.provider = "y"  # type: ignore[misc]


class TestProviderHealth:
    def test_health_default_up(self):
        h = ProviderHealth()
        assert h.up is True
        assert h.latency_ms_p50 is None
        assert h.error_rate == 0.0

    def test_health_degraded(self):
        h = ProviderHealth(up=True, latency_ms_p50=2500, error_rate=0.15)
        # Pure dataclass — degradation policy is applied by the strategy.
        assert h.error_rate == 0.15


class TestUserModelPolicy:
    def test_default_empty(self):
        p = UserModelPolicy()
        assert p.prefer == ()
        assert p.forbid == ()

    def test_with_preferences(self):
        p = UserModelPolicy(prefer=("private-qwen", "anthropic-haiku"))
        assert p.prefer == ("private-qwen", "anthropic-haiku")


class TestCohortModelPolicy:
    def test_default_empty(self):
        p = CohortModelPolicy()
        assert p.forbidden_providers == ()
        assert p.required_provider_class is None

    def test_with_forbidden(self):
        p = CohortModelPolicy(forbidden_providers=("openai-gpt4-turbo",))
        assert "openai-gpt4-turbo" in p.forbidden_providers


class TestModelContext:
    def test_minimal_context(self):
        ctx = ModelContext(
            classification=_classification(),
            budget_remaining_usd=10.0,
            network_reachable=frozenset({"public", "private"}),
            user_policy=UserModelPolicy(),
            cohort_policy=CohortModelPolicy(),
            available_providers={},
        )
        assert ctx.budget_remaining_usd == 10.0
        assert "public" in ctx.network_reachable

    def test_context_with_providers(self):
        ctx = ModelContext(
            classification=_classification(),
            budget_remaining_usd=10.0,
            network_reachable=frozenset({"public"}),
            user_policy=UserModelPolicy(),
            cohort_policy=CohortModelPolicy(),
            available_providers={
                "anthropic": ProviderHealth(up=True),
                "ollama": ProviderHealth(up=False),
            },
        )
        assert ctx.available_providers["anthropic"].up is True
        assert ctx.available_providers["ollama"].up is False

    def test_context_is_frozen(self):
        ctx = ModelContext(
            classification=_classification(),
            budget_remaining_usd=10.0,
            network_reachable=frozenset(),
            user_policy=UserModelPolicy(),
            cohort_policy=CohortModelPolicy(),
            available_providers={},
        )
        with pytest.raises((AttributeError, TypeError)):
            ctx.budget_remaining_usd = 5.0  # type: ignore[misc]


class TestResolvedAssembly:
    def test_assembly_basic(self):
        choice = ProviderChoice(
            provider="anthropic", model="claude-sonnet-4", tier="public",
            estimated_cost_usd=0.005,
        )
        assembly = ResolvedAssembly(
            by_role={ModelRole.EXECUTOR: choice},
            rationale="user.prefer.anthropic",
            estimated_cost_usd=0.005,
            classification_clamp=_classification(),
        )
        assert assembly.by_role[ModelRole.EXECUTOR] == choice
        assert assembly.estimated_cost_usd == 0.005

    def test_assembly_audit_payload(self):
        """The assembly should serialize cleanly for audit-trail capture."""
        choice = ProviderChoice(
            provider="private-qwen", model="qwen-32b", tier="private",
            estimated_cost_usd=0.0, via="federation:example-org",
        )
        assembly = ResolvedAssembly(
            by_role={ModelRole.EXECUTOR: choice},
            rationale="classification.unclassified + budget",
            estimated_cost_usd=0.0,
            classification_clamp=_classification(),
        )
        payload = assembly.to_audit_payload()
        assert "model_strategy_resolved" in payload or "resolved" in payload
        assert payload["estimated_cost_usd"] == 0.0
        assert "executor" in payload["resolved"]
        assert payload["resolved"]["executor"]["via"] == "federation:example-org"
