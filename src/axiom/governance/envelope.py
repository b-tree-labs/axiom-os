# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ActionEnvelope — the universal currency of every governed action.

Per spec-governance-fabric §1: every action that crosses a trust,
classification, or ownership boundary carries this envelope. Each primitive
(authz / vault / notifications / schedule) consumes the same shape.

Construction is intentionally permissive in development (so call sites
can build envelopes without scaffolding the full ontology); the binding
gate is the static-analysis lint that runs in CI. ``strict=True`` mimics
the lint's runtime check for unit tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from axiom.governance.capability import CapabilityToken
from axiom.governance.classification import Classification
from axiom.governance.intent import ActionIntent
from axiom.governance.provenance import ProvenanceRef
from axiom.governance.resource import ResourceRef
from axiom.governance.subject import SubjectContext
from axiom.vega.identity.principal import Principal


@dataclass(frozen=True)
class ActionEnvelope:
    """The universal envelope every governed action carries.

    Fields follow spec §1.1 verbatim. ``strict=True`` enforces the
    registered-intent check at construction; production call sites are
    expected to use the lint instead.
    """

    actor: Principal
    capability: CapabilityToken
    classification: Classification
    context_fragment_id: str
    """Identifier of the memory context this action runs under."""
    provenance_parent: ProvenanceRef
    federation_origin: str | None
    """``None`` if locally originated; peer id if forwarded by federation."""
    intent: ActionIntent
    resource: ResourceRef
    deadline: datetime | None
    dedup_key: str

    # Construction-time gate; defaults off for permissive dev builds.
    strict: bool = False

    # The authorization-substrate view of the actor (ADR-083/084). Optional so
    # envelopes built before identity resolution carry an empty subject and the
    # substrate ABSTAINs. Populated at the identity boundary from token claims.
    subject: SubjectContext | None = None

    def __post_init__(self) -> None:
        if not self.dedup_key:
            raise ValueError("ActionEnvelope.dedup_key cannot be empty")
        if self.strict and not self.intent.is_registered():
            raise ValueError(
                f"unregistered intent {self.intent.value!r}; "
                "register via axiom.governance.intent.register_intent "
                "or extend REGISTERED_INTENTS"
            )

    @property
    def is_local(self) -> bool:
        return self.federation_origin is None

    def to_dict(self) -> dict:
        """Serialize for receipt fragments (spec §4.1)."""
        d: dict = {
            "actor": self.actor.handle,
            "capability_id": self.capability.id,
            "classification": self.classification.value,
            "context_fragment_id": self.context_fragment_id,
            "provenance_parent": str(self.provenance_parent),
            "intent": self.intent.value,
            "resource": str(self.resource),
            "dedup_key": self.dedup_key,
            "federation_origin": self.federation_origin,
        }
        if self.deadline is not None:
            d["deadline"] = self.deadline.isoformat()
        if self.subject is not None:
            d["subject"] = self.subject.to_dict()
        return d


__all__ = ["ActionEnvelope"]
