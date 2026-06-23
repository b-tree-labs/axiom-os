# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Tests for trait routing decisions per spec §10 (decision table).

The decision table is small + load-bearing: every cell of the
{deterministic, stochastic, hybrid} x {cache_lookup, cache_write,
retry_seed_strategy, on_corruption} matrix gets covered here.

Trait routing is "real" code (not stub). The dispatcher consults
the policy module; the policy is a pure function returning a
RoutingDecision.
"""

from __future__ import annotations

import pytest

from axiom.compute_decomposition.routing import (
    RoutingDecision,
    SeedStrategy,
    routing_policy_for_trait,
)
from axiom.compute_decomposition.types import Trait


def test_deterministic_uses_cache_lookup_and_write():
    decision: RoutingDecision = routing_policy_for_trait(Trait.DETERMINISTIC)
    assert decision.cache_lookup_before_dispatch is True
    assert decision.cache_write_on_success is True


def test_stochastic_skips_cache_entirely():
    decision = routing_policy_for_trait(Trait.STOCHASTIC)
    assert decision.cache_lookup_before_dispatch is False
    assert decision.cache_write_on_success is False


def test_hybrid_caches_deterministic_prefix_only():
    decision = routing_policy_for_trait(Trait.HYBRID)
    assert decision.cache_lookup_before_dispatch is True
    assert decision.cache_write_on_success is True
    assert decision.deterministic_prefix_only is True


def test_deterministic_retry_uses_same_cache_key():
    decision = routing_policy_for_trait(Trait.DETERMINISTIC)
    assert decision.retry_seed_strategy is SeedStrategy.SAME_CACHE_KEY


def test_stochastic_retry_uses_new_seed():
    decision = routing_policy_for_trait(Trait.STOCHASTIC)
    assert decision.retry_seed_strategy is SeedStrategy.NEW_SEED


def test_hybrid_retry_recomputes_prefix_then_reseeds():
    decision = routing_policy_for_trait(Trait.HYBRID)
    assert decision.retry_seed_strategy is SeedStrategy.PREFIX_THEN_NEW_SEED


def test_deterministic_corruption_keeps_cache_key_and_penalizes_leaf():
    decision = routing_policy_for_trait(Trait.DETERMINISTIC)
    on_corruption = decision.on_output_corruption
    assert on_corruption.reassign is True
    assert on_corruption.preserve_cache_key is True
    assert on_corruption.trust_penalty is True


def test_stochastic_corruption_voids_seed_and_reassigns_with_new_seed():
    decision = routing_policy_for_trait(Trait.STOCHASTIC)
    on_corruption = decision.on_output_corruption
    assert on_corruption.reassign is True
    assert on_corruption.preserve_cache_key is False
    # void the original contribution; a new seed claims the chunk
    assert on_corruption.void_original is True


def test_hybrid_corruption_rejects_entire_result():
    decision = routing_policy_for_trait(Trait.HYBRID)
    on_corruption = decision.on_output_corruption
    assert on_corruption.reject_entire_result is True


def test_unknown_trait_raises():
    with pytest.raises(ValueError):
        routing_policy_for_trait("not_a_trait")  # type: ignore[arg-type]


def test_routing_decision_is_immutable():
    """Decisions are pure data; mutating them must fail loudly so
    that no consumer ever drifts the policy at the call site."""
    decision = routing_policy_for_trait(Trait.DETERMINISTIC)
    with pytest.raises((AttributeError, TypeError)):
        decision.cache_lookup_before_dispatch = False  # type: ignore[misc]
