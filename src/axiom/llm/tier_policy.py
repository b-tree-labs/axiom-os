# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Which providers may hold which content (ADR-054, ADR-168, issue #1136).

One rule, used by the model switch and by provider selection, so they cannot disagree:

    content tier       admitted by provider tier
    -----------------  -----------------------------------------
    public / any       public, any, export_controlled
    export_controlled  export_controlled ONLY (``any`` is not cleared for controlled content)
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

CONTROLLED = "export_controlled"


def admits(provider_tier: str, content_tier: str) -> bool:
    """True when a provider of ``provider_tier`` may receive content of ``content_tier``."""
    if content_tier == CONTROLLED:
        return provider_tier == CONTROLLED
    return True


@dataclass(frozen=True)
class SwitchDecision:
    allowed: bool
    provider: str
    content_tier: str
    reason: str
    admitted: tuple[
        str, ...
    ] = ()  # the providers that WOULD be admitted, so a refusal is actionable


class ProviderNotAdmitted(PermissionError):
    """Raised by a provider switch the content's tier forbids. Carries the decision."""

    def __init__(self, decision: SwitchDecision):
        super().__init__(decision.reason)
        self.decision = decision


def decide_switch(providers: Iterable, name: str, content_tier: str = "any") -> SwitchDecision:
    """Whether the session may switch to provider ``name``, given the tier of what it already holds."""
    listed = list(providers)
    target = next((p for p in listed if p.name == name), None)
    if target is None:
        return SwitchDecision(False, name, content_tier, f"no provider named {name!r}")
    admitted = tuple(p.name for p in listed if admits(p.routing_tier, content_tier))
    if admits(target.routing_tier, content_tier):
        return SwitchDecision(True, name, content_tier, "admitted", admitted)
    options = ", ".join(admitted) if admitted else "none is configured"
    return SwitchDecision(
        False,
        name,
        content_tier,
        f"{name} (tier {target.routing_tier}) may not receive {content_tier} content that this session "
        f"already holds. Providers that may: {options}.",
        admitted,
    )
