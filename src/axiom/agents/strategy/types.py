# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ModelStrategy primitive types — spec-model-routing §13.2."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from axiom.vega.federation.policy import ClassificationStamp


class ModelRole(str, Enum):
    """Role a model plays inside a step's assembly.

    See spec-model-routing §13.2. Existing single-model call sites use
    ``DEFAULT`` for backward compatibility with the §§2–4 QueryRouter.
    """

    ROUTER = "router"
    PLANNER = "planner"
    EXECUTOR = "executor"
    VERIFIER = "verifier"
    EMBED = "embed"
    RERANK = "rerank"
    DEFAULT = "default"


@dataclass(frozen=True)
class ProviderChoice:
    """A concrete provider chosen for a role."""

    provider: str
    model: str
    tier: str                              # "public" / "private" / "any"
    estimated_cost_usd: float
    via: str = "local"                     # "local" or "federation:<peer>"


@dataclass(frozen=True)
class ProviderHealth:
    up: bool = True
    latency_ms_p50: int | None = None
    error_rate: float = 0.0


@dataclass(frozen=True)
class UserModelPolicy:
    prefer: tuple[str, ...] = ()
    forbid: tuple[str, ...] = ()


@dataclass(frozen=True)
class CohortModelPolicy:
    forbidden_providers: tuple[str, ...] = ()
    required_provider_class: str | None = None    # e.g., "private-only"
    cui_providers: tuple[str, ...] = ()              # whitelist for CUI inputs


@dataclass(frozen=True)
class FederatedInferenceEndpoint:
    """A federation-resolvable inference endpoint per ADR-030."""

    peer_id: str
    provider: str
    model: str
    estimated_cost_usd: float = 0.0


@dataclass(frozen=True)
class ModelContext:
    """Runtime context the strategy consults at resolve time.

    Per spec-model-routing §13.3 resolution rules.
    """

    classification: ClassificationStamp
    budget_remaining_usd: float
    network_reachable: frozenset[str]                # subset of {"public","private","federation"}
    user_policy: UserModelPolicy
    cohort_policy: CohortModelPolicy
    available_providers: Mapping[str, ProviderHealth]
    federation_peers: Sequence[FederatedInferenceEndpoint] | None = None
    # §14.3 tier-hint: caller-supplied difficulty hint; strategy may use as
    # a tiebreaker within survivors. Vocabulary: simple|standard|smart|smartest
    # per [[feedback_llm_tier_policy_axiom_primitive]]. None → infer (§13.3 step 5).
    tier_hint: str | None = None


@dataclass(frozen=True)
class ResolvedAssembly:
    """The assembly chosen for a set of roles, with audit metadata."""

    by_role: Mapping[ModelRole, ProviderChoice]
    rationale: str
    estimated_cost_usd: float
    classification_clamp: ClassificationStamp

    def to_audit_payload(self) -> dict:
        """Render for capture into a plan / agent-run fragment audit trail."""
        return {
            "model_strategy_resolved": True,
            "rationale": self.rationale,
            "estimated_cost_usd": self.estimated_cost_usd,
            "classification_clamp": self.classification_clamp.level
            if hasattr(self.classification_clamp, "level")
            else str(self.classification_clamp),
            "resolved": {
                role.value: {
                    "provider": choice.provider,
                    "model": choice.model,
                    "tier": choice.tier,
                    "via": choice.via,
                    "estimated_cost_usd": choice.estimated_cost_usd,
                }
                for role, choice in self.by_role.items()
            },
        }


# ---------------------------------------------------------------------------
# Provider catalog — minimal in-memory representation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderSpec:
    """A provider entry the strategy can resolve to.

    The runtime ``models.toml`` is parsed into a sequence of these. Strategies
    receive the parsed list via the registry; tests inject their own.
    """

    name: str
    tier: str                              # "public" / "private" / "any"
    model: str
    estimated_cost_per_1k_tokens_usd: float = 0.0
    role_hints: tuple[ModelRole, ...] = ()    # e.g., (ROUTER, EMBED) for cheap models
    requires_vpn: bool = False
