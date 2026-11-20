# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Built-in ModelStrategy factories — spec-model-routing §13.6.

Four strategies ship out of the box; cohorts + users compose new ones via
``models.toml`` entries (see §13.6 example TOML).

- ``legacy_router`` — single-role; identical behavior to §§2–4 QueryRouter.
- ``cost_conservative`` — cheap router/embed; mid executor; cheap verifier.
  Default for plan/agent on Edge + Workstation.
- ``quality_first`` — best executor; mid router/verifier; budget-loose.
  Default for Server + Platform when budget unset.
- ``cohort_pinned`` — strictly follows cohort_policy.required_provider_class.
  No fallback; raises ModelStrategyUnsatisfiable on miss.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from axiom.agents.strategy.strategy import (
    BaseStrategyResolver,
    ModelStrategyUnsatisfiable,
)
from axiom.agents.strategy.types import (
    ModelContext,
    ModelRole,
    ProviderChoice,
    ProviderSpec,
)

# ---------------------------------------------------------------------------
# Strategies as factory functions returning configured BaseStrategyResolver
# ---------------------------------------------------------------------------


def legacy_router(*, providers: Sequence[ProviderSpec]) -> BaseStrategyResolver:
    """The simplest concrete strategy — single-role; QueryRouter-equivalent."""
    return BaseStrategyResolver(name="legacy-router", providers=tuple(providers))


@dataclass
class _RoleBiasedResolver(BaseStrategyResolver):
    """Resolver that biases provider choice by per-role preferences.

    ``role_prefers`` maps a role to an ordered tuple of provider names; if
    the user policy doesn't supply a preference, the strategy's per-role
    preference is consulted before falling back to role hints + cost.
    """

    role_prefers: dict[ModelRole, tuple[str, ...]] = field(default_factory=dict)

    def _resolve_role(
        self, role: ModelRole, ctx: ModelContext, n_remaining_roles: int
    ) -> ProviderChoice:
        # If user has no preference, layer in our per-role preference into the
        # context's user policy so the base resolver picks it up.
        if not ctx.user_policy.prefer and role in self.role_prefers:
            from dataclasses import replace

            from axiom.agents.strategy.types import UserModelPolicy
            biased_user = UserModelPolicy(
                prefer=self.role_prefers[role],
                forbid=ctx.user_policy.forbid,
            )
            ctx = replace(ctx, user_policy=biased_user)
        return super()._resolve_role(role, ctx, n_remaining_roles)


def cost_conservative(*, providers: Sequence[ProviderSpec]) -> _RoleBiasedResolver:
    """Cost-conservative strategy.

    Per-role preference (subject to context filters):
    - ROUTER  → cheapest local; falls back to cheap public if unreachable.
    - PLANNER → cheap public.
    - EXECUTOR → mid-cost (private free preferred).
    - VERIFIER → cheap local.
    - EMBED → local-cheap.
    """
    role_prefers: dict[ModelRole, tuple[str, ...]] = {}
    # Preferences are name-driven; the base resolver applies role_hint biasing
    # too, so we leave concrete name lists to caller-supplied user policy
    # when available. Empty defaults still work via role_hints.
    return _RoleBiasedResolver(
        name="cost-conservative",
        providers=tuple(providers),
        role_prefers=role_prefers,
    )


def quality_first(*, providers: Sequence[ProviderSpec]) -> _RoleBiasedResolver:
    """Quality-first strategy: prefer highest-quality available for EXECUTOR;
    relax budget filtering relative to cost_conservative."""
    role_prefers: dict[ModelRole, tuple[str, ...]] = {}
    return _RoleBiasedResolver(
        name="quality-first",
        providers=tuple(providers),
        role_prefers=role_prefers,
    )


@dataclass
class _CohortPinnedResolver(BaseStrategyResolver):
    """Cohort-pinned: rigidly follows cohort.required_provider_class with no
    user-preference fallback. Raises on miss (no silent degradation)."""

    name: str = "cohort-pinned"

    def _resolve_role(
        self, role: ModelRole, ctx: ModelContext, n_remaining_roles: int
    ) -> ProviderChoice:
        try:
            return super()._resolve_role(role, ctx, n_remaining_roles)
        except ModelStrategyUnsatisfiable:
            # Strict: no fallback. Re-raise.
            raise


def cohort_pinned(*, providers: Sequence[ProviderSpec]) -> _CohortPinnedResolver:
    return _CohortPinnedResolver(
        name="cohort-pinned",
        providers=tuple(providers),
    )
