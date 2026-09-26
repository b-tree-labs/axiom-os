# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Trait-routing policy per spec §10.

The decision table is a small piece of load-bearing logic; making it
a pure function keeps it auditable + testable + impossible to drift
across call sites.

Phase A ships the policy + a smoke-test integration with the runner;
Phase B adds the chaos-leaf-failure integration tests against a
multi-node simulator.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .types import Trait


__all__ = [
    "RoutingDecision",
    "OnCorruptionPolicy",
    "SeedStrategy",
    "routing_policy_for_trait",
]


class SeedStrategy(Enum):
    """How a retry derives its seed.

    - SAME_CACHE_KEY: deterministic chunks reuse the cache key; first
      success closes the chunk.
    - NEW_SEED: stochastic chunks get f(seed_seed, sequence_index,
      retry_count); the original seed's contribution is voided from
      the aggregation pool.
    - PREFIX_THEN_NEW_SEED: hybrid chunks recompute the deterministic
      prefix from cache (or re-run) and re-seed only the stochastic
      suffix.
    """

    SAME_CACHE_KEY = "same_cache_key"
    NEW_SEED = "new_seed"
    PREFIX_THEN_NEW_SEED = "prefix_then_new_seed"


@dataclass(frozen=True)
class OnCorruptionPolicy:
    """How the router responds when an output artifact's content hash
    fails to verify on fetch (or the leaf returns demonstrably wrong
    output)."""

    reassign: bool = True
    preserve_cache_key: bool = False
    void_original: bool = False
    reject_entire_result: bool = False
    trust_penalty: bool = False


@dataclass(frozen=True)
class RoutingDecision:
    """The full per-trait policy. Frozen so callers can't drift it."""

    trait: Trait
    cache_lookup_before_dispatch: bool
    cache_write_on_success: bool
    deterministic_prefix_only: bool
    retry_seed_strategy: SeedStrategy
    on_output_corruption: OnCorruptionPolicy


# ---------------------------------------------------------------------------
# Policy table (spec §10)
# ---------------------------------------------------------------------------


_POLICY: dict[Trait, RoutingDecision] = {
    Trait.DETERMINISTIC: RoutingDecision(
        trait=Trait.DETERMINISTIC,
        cache_lookup_before_dispatch=True,
        cache_write_on_success=True,
        deterministic_prefix_only=False,
        retry_seed_strategy=SeedStrategy.SAME_CACHE_KEY,
        on_output_corruption=OnCorruptionPolicy(
            reassign=True,
            preserve_cache_key=True,
            void_original=False,
            reject_entire_result=False,
            trust_penalty=True,
        ),
    ),
    Trait.STOCHASTIC: RoutingDecision(
        trait=Trait.STOCHASTIC,
        cache_lookup_before_dispatch=False,
        cache_write_on_success=False,
        deterministic_prefix_only=False,
        retry_seed_strategy=SeedStrategy.NEW_SEED,
        on_output_corruption=OnCorruptionPolicy(
            reassign=True,
            preserve_cache_key=False,
            void_original=True,
            reject_entire_result=False,
            trust_penalty=True,
        ),
    ),
    Trait.HYBRID: RoutingDecision(
        trait=Trait.HYBRID,
        cache_lookup_before_dispatch=True,
        cache_write_on_success=True,
        deterministic_prefix_only=True,
        retry_seed_strategy=SeedStrategy.PREFIX_THEN_NEW_SEED,
        on_output_corruption=OnCorruptionPolicy(
            reassign=True,
            preserve_cache_key=False,
            void_original=False,
            reject_entire_result=True,
            trust_penalty=True,
        ),
    ),
}


def routing_policy_for_trait(trait: Trait) -> RoutingDecision:
    if not isinstance(trait, Trait):
        raise ValueError(
            f"trait must be a Trait enum member; got {trait!r}"
        )
    return _POLICY[trait]
