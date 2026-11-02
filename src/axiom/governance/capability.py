# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CapabilityToken — scoped, time-limited, revocable authority.

Per spec-governance-fabric §2: every authenticated action presents a
capability token. The token is cryptographically bound to its issuer
(KEEP / vault), scoped to a verb + resource pattern, classification-
ceiling-enforced, and time-bounded.

This module defines the type contract — KEEP's `axiom.extensions.builtins.
vault` module owns issuance, signature verification, revocation lifecycle,
and the outbound-call chokepoint.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from axiom.governance.budget import BudgetEnvelope, BudgetLedger, intersect_request
from axiom.governance.classification import (
    Classification,
    classification_lte,
)
from axiom.governance.intent import ActionIntent, IntentPattern
from axiom.governance.resource import ResourcePattern, ResourceRef
from axiom.vega.identity.principal import Principal


@dataclass(frozen=True)
class CapabilityToken:
    """A scoped, time-limited, revocable assertion of authority.

    Per spec §2.1: the token names exactly what action it permits, on
    what resource, until when, and is revocable independently of the
    underlying credential it dereferences to.
    """

    id: str
    """Stable identifier — uuidv7 in production."""

    issuer: Principal
    """The vault that minted this token."""

    subject: Principal
    """Who may present this token (the actor)."""

    intent_pattern: IntentPattern
    resource_pattern: ResourcePattern
    classification_ceiling: Classification

    not_before: datetime
    not_after: datetime

    delegation_depth: int
    """How many further delegations are permitted; 0 = leaf."""

    parent_capability: str | None
    """The parent token's id if this token was delegated."""

    signature: bytes
    """Issuer's signature over the canonical encoding. KEEP verifies."""

    budget: BudgetEnvelope | None = None
    """How much the holder may consume (ADR-232 part 5, prd-capability-budgets).

    ``None`` means unbounded, which is valid only for a token that does not back
    an agent run: an agent-backed run refuses to start without an envelope. The
    grant is immutable; what has been spent lives in a :class:`BudgetLedger`."""

    def permits_intent(self, intent: ActionIntent) -> bool:
        return self.intent_pattern.matches(intent)

    def permits_resource(self, resource: ResourceRef) -> bool:
        return self.resource_pattern.matches(resource)

    def permits_classification(self, classification: Classification) -> bool:
        return classification_lte(classification, self.classification_ceiling)

    def is_valid_at(self, when: datetime) -> bool:
        return self.not_before <= when <= self.not_after

    @property
    def can_delegate(self) -> bool:
        return self.delegation_depth > 0

    # ------------------------------------------------------------------
    # Budget envelope (A0). The token stays frozen: spending is tracked in a
    # ledger, and a delegated token is a new token, never an edit.
    # ------------------------------------------------------------------

    @property
    def has_budget(self) -> bool:
        return self.budget is not None

    def with_budget(self, envelope: BudgetEnvelope | None) -> CapabilityToken:
        """A copy of this token carrying ``envelope``."""
        return replace(self, budget=envelope)

    def budget_ledger(self) -> BudgetLedger | None:
        """A fresh ledger over this token's envelope, or ``None`` when it has none."""
        return None if self.budget is None else BudgetLedger(self.budget)

    def delegate_with_budget(
        self,
        requested: BudgetEnvelope | None = None,
        *,
        remaining: BudgetEnvelope | None = None,
        subject: Principal | None = None,
    ) -> CapabilityToken:
        """The token a child receives: a new id and an envelope that can only lower.

        The child's envelope is ``requested`` intersected with ``remaining`` (the
        parent's remaining amounts from its ledger; the parent's full grant when
        omitted). A parent with no envelope is unbounded, so the child gets
        exactly what it asked for. Re-signing is the issuer's job (KEEP): the
        child carries an empty signature until it is issued.
        """
        if not self.can_delegate:
            raise ValueError("token has no delegation depth left")
        ceiling = remaining if remaining is not None else self.budget
        child_budget = requested if ceiling is None else intersect_request(ceiling, requested)
        from axiom.infra.identifiers import generate_id

        return replace(
            self,
            id=generate_id(),
            subject=subject if subject is not None else self.subject,
            delegation_depth=self.delegation_depth - 1,
            parent_capability=self.id,
            signature=b"",
            budget=child_budget,
        )

    # ------------------------------------------------------------------
    # Test helper. Keeps test-fixtures localized; KEEP's real issuer
    # never calls this.
    # ------------------------------------------------------------------

    @classmethod
    def unscoped_test_token(cls, *, subject: Principal) -> CapabilityToken:
        """A wildcard-scope token for tests + scaffolding only.

        The real KEEP issuer narrows scope at every issuance. Lint
        catches use of this helper outside ``tests/`` and the
        ``conftest.py`` scaffolding path.
        """
        now = datetime.now(UTC)
        return cls(
            id="00000000000000000000000000",
            issuer=Principal(handle="@test:fixture", public_bytes=b"\x00" * 32),
            subject=subject,
            intent_pattern=IntentPattern("*"),
            resource_pattern=ResourcePattern("*"),
            classification_ceiling=Classification.CONTROLLED,
            not_before=now - timedelta(seconds=1),
            not_after=now + timedelta(hours=1),
            delegation_depth=0,
            parent_capability=None,
            signature=b"\x00" * 64,
        )


__all__ = ["CapabilityToken"]
