# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The authorization-substrate seam GUARD calls (ADR-083).

GUARD stays the sole decision point. Fine-grained authorization — RBAC as role
relations, ReBAC as tuples, ABAC as CEL conditions — is delegated to a *substrate*
that ``decide()`` consults as one source, **under** the deterministic capability
floor (which runs first and fail-closed). OpenFGA is the first real substrate
(ADR-083, build P2); this module is the port + the fail-safe defaults so the seam
exists and is testable before OpenFGA is wired.

Three-valued on purpose:

- ``ALLOW``   — the substrate affirmatively grants. For P1 this is *not* an
  auto-permit: it lets the decision continue through the existing rule +
  graduation pipeline. The combiner that makes ALLOW authoritative arrives with
  the ``PolicySourceRegistry`` (P2/P3).
- ``DENY``    — the substrate refuses. Deny-overrides: ``decide()`` stops here.
- ``ABSTAIN`` — no opinion (resource not modelled yet). The decision falls
  through to rules + graduation, so phased rollout never breaks novel actions.

Extensions never call a substrate directly — only via ``GUARD.decide()``.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol, runtime_checkable

from axiom.governance import ActionEnvelope


class SubstrateDecision(Enum):
    ALLOW = "allow"
    DENY = "deny"
    ABSTAIN = "abstain"


@runtime_checkable
class AuthzSubstrate(Protocol):
    """A fine-grained authorization backend GUARD consults.

    Implementations map the envelope's ``resource``/``intent``/``subject`` to a
    substrate query (for OpenFGA: object, relation, user + contextual tuples) and
    return a three-valued decision. They MUST NOT raise for an un-modelled
    resource — return ``ABSTAIN`` — and SHOULD fail closed (``DENY`` or a raised
    error caught by ``decide()``) on backend unavailability when the deployment
    mandates a substrate.
    """

    def check(self, envelope: ActionEnvelope) -> SubstrateDecision: ...


class NullSubstrate:
    """The default when no substrate is wired: ``ABSTAIN`` on everything.

    Abstention means *no opinion* — not permit-all and not deny-all — so GUARD's
    deterministic capability floor + rule engine + graduation govern exactly as
    they did before ADR-083. This keeps ``decide()`` behaviour-preserving until a
    real substrate is registered. A deployment that *requires* substrate coverage
    should register ``DenyAllSubstrate`` (or a real backend) so un-modelled
    actions are denied rather than silently abstained.
    """

    def check(self, envelope: ActionEnvelope) -> SubstrateDecision:
        return SubstrateDecision.ABSTAIN


class DenyAllSubstrate:
    """Explicit fail-closed stance: deny everything the substrate is asked about.

    Use when a substrate is mandated but the real backend is unavailable, so an
    un-modelled or unreachable decision denies rather than abstains.
    """

    def check(self, envelope: ActionEnvelope) -> SubstrateDecision:
        return SubstrateDecision.DENY


__all__ = [
    "AuthzSubstrate",
    "DenyAllSubstrate",
    "NullSubstrate",
    "SubstrateDecision",
]
