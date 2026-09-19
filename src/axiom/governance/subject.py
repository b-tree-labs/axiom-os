# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""SubjectContext — the authorization-substrate view of the actor (ADR-083/084).

GUARD's deterministic capability floor works from the ``Principal`` alone. The
fine-grained substrate (OpenFGA, ADR-083) needs more: which tenant scopes the
decision, which substrate user id the actor maps to, the attributes an ABAC (CEL)
condition reads, and any ephemeral relationship tuples supplied per request.

That extra shape rides on the ``ActionEnvelope`` as an optional ``subject``.
It is deliberately separate from ``Principal`` (kept minimal, its bytes are
load-bearing in capability signatures) and from the future ``ActorContext``
(ADR-084, which unifies roles/tenant/assurance) — ``SubjectContext`` is only what
the substrate ``Check`` consumes. Until ActorContext lands, a resolver at the
identity boundary populates it from verified token claims.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

#: A contextual relationship tuple supplied for a single Check — (user,
#: relation, object), e.g. ("user:alex", "member", "team:rsc"). Ephemeral: not
#: persisted in the substrate store, only asserted for this decision.
ContextualTuple = tuple[str, str, str]


@dataclass(frozen=True)
class SubjectContext:
    """What the authorization substrate needs beyond the bare principal.

    All fields optional so an envelope built before identity resolution (or in a
    single-tenant deployment with no substrate) carries an empty subject and the
    substrate simply ``ABSTAIN``s.
    """

    tenant: str | None = None
    """Multi-tenancy scope. ``None`` in single-tenant deployments."""
    fga_user: str | None = None
    """The substrate's user identifier (e.g. ``user:@alex:example-org``)."""
    attributes: Mapping[str, Any] = field(default_factory=dict)
    """Request attributes an ABAC/CEL condition may read (e.g. on-campus, MFA)."""
    contextual_tuples: tuple[ContextualTuple, ...] = ()
    """Ephemeral relationship tuples asserted for this decision only."""

    def to_dict(self) -> dict:
        """Receipt-friendly projection (attributes/tuples omitted for brevity)."""
        return {"tenant": self.tenant, "fga_user": self.fga_user}


__all__ = ["ContextualTuple", "SubjectContext"]
