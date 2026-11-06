# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""OpenFgaSubstrate — the relationship-based authorization backend (ADR-083).

GUARD's :class:`SubstrateSource` consults an :class:`AuthzSubstrate`; this is the
OpenFGA implementation. It maps an :class:`ActionEnvelope` to an OpenFGA check —
``(user, relation, object)`` plus contextual tuples — and translates the result
into the substrate's three-valued opinion:

* a ``blocked`` relation on the object  → **DENY**   (explicit, deny-overrides)
* the permit relation holds             → **ALLOW**  (authoritative grant)
* neither                               → **ABSTAIN** (no relationship; defer)

Absence of a grant is ABSTAIN, *not* DENY — so an action OpenFGA has not modelled
falls through to the rule engine and the deterministic floor, rather than being
silently denied. Explicit denial is a separate ``blocked`` relation.

**Deferred (infra-gated).** The live client adapter over ``openfga-sdk`` and the
recall/latency benchmark gate need an OpenFGA-on-Postgres server, which the build
sandbox lacks. The substrate here is written against an injected
:class:`FgaCheckClient` seam so its logic is fully unit-tested without a server;
wiring the real SDK client is a thin adapter that lands with the server.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from axiom.extensions.builtins.authz.substrate import SubstrateDecision

if TYPE_CHECKING:
    from axiom.governance import ActionEnvelope
    from axiom.governance.subject import ContextualTuple

#: Convention for the relation that models an explicit block (deny-overrides).
BLOCKED_RELATION = "blocked"


@runtime_checkable
class FgaCheckClient(Protocol):
    """The one OpenFGA operation the substrate needs: a relationship check.

    Kept synchronous and minimal so a fake satisfies it in tests and a thin
    adapter over ``openfga-sdk``'s client satisfies it in production.
    """

    def check(
        self,
        *,
        user: str,
        relation: str,
        object: str,
        contextual_tuples: tuple[ContextualTuple, ...] = (),
    ) -> bool: ...


@dataclass(frozen=True)
class FgaCheckSpec:
    """The OpenFGA check(s) an envelope maps to.

    ``permit_relation`` grants ALLOW; ``deny_relation`` (when set) grants DENY and
    is checked first. ``contextual_tuples`` are request-time tuples (e.g. "alice
    is on-call right now") evaluated alongside stored ones.
    """

    user: str
    object: str
    permit_relation: str
    deny_relation: str | None = None
    contextual_tuples: tuple[ContextualTuple, ...] = field(default_factory=tuple)


#: An envelope → check-spec mapping. Returns ``None`` to opt out (→ ABSTAIN).
TupleMapper = Callable[["ActionEnvelope"], "FgaCheckSpec | None"]


def default_mapper(envelope: ActionEnvelope) -> FgaCheckSpec | None:
    """The starter envelope→tuple mapping.

    * user: ``subject.fga_user`` when present, else ``user:<actor handle>``.
    * object: ``<scheme>:<identifier>`` from the resource.
    * permit relation: the dotted intent with ``.`` → ``_`` (a valid relation name).
    * deny relation: ``blocked``.

    Deployments whose ``.fga`` model uses different types/relations register their
    own mapper; this is the transparent default (intent *is* the relation).
    """
    subject = envelope.subject
    if subject is not None and subject.fga_user:
        user = subject.fga_user
    else:
        user = f"user:{envelope.actor.handle}"
    resource = envelope.resource
    obj = f"{resource.scheme}:{resource.identifier}"
    relation = envelope.intent.value.replace(".", "_")
    contextual = subject.contextual_tuples if subject is not None else ()
    return FgaCheckSpec(
        user=user,
        object=obj,
        permit_relation=relation,
        deny_relation=BLOCKED_RELATION,
        contextual_tuples=contextual,
    )


@dataclass
class OpenFgaSubstrate:
    """An :class:`AuthzSubstrate` backed by OpenFGA relationship checks.

    ``on_error`` is the opinion returned when the client raises. It defaults to
    ABSTAIN — an OpenFGA outage must not brick every authorization (the rule
    engine and capability floor still apply); a strict deployment sets it to
    ``SubstrateDecision.DENY`` to fail closed instead.
    """

    client: FgaCheckClient
    mapper: TupleMapper = default_mapper
    on_error: SubstrateDecision = SubstrateDecision.ABSTAIN

    def check(self, envelope: ActionEnvelope) -> SubstrateDecision:
        spec = self.mapper(envelope)
        if spec is None:
            return SubstrateDecision.ABSTAIN
        try:
            if spec.deny_relation and self._holds(spec, spec.deny_relation):
                return SubstrateDecision.DENY
            if self._holds(spec, spec.permit_relation):
                return SubstrateDecision.ALLOW
            return SubstrateDecision.ABSTAIN
        except Exception:
            return self.on_error

    def _holds(self, spec: FgaCheckSpec, relation: str) -> bool:
        return self.client.check(
            user=spec.user,
            relation=relation,
            object=spec.object,
            contextual_tuples=spec.contextual_tuples,
        )


__all__ = [
    "BLOCKED_RELATION",
    "FgaCheckClient",
    "FgaCheckSpec",
    "OpenFgaSubstrate",
    "TupleMapper",
    "default_mapper",
]
