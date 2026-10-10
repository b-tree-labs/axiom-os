# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Membership → ``SubjectContext`` — the identity-boundary binding.

``governance/subject.py`` names this contract directly: *"Until ActorContext
lands, a resolver at the identity boundary populates it from verified token
claims."* This is that resolver, and it is where the directory seam finally
reaches GUARD.

Two properties are load-bearing.

**Groups ride as contextual tuples.** They are asserted for a single decision
rather than read from the store, so authorization works from the moment
resolution does — no dependency on the reconciler having run, and no OpenFGA
server needed for the seam to be useful. The reconciler's persisted tuples and
these ephemeral ones agree by construction because both use the same prefixes.

**A degraded projection contributes no grants.** Contextual tuples *are* grants;
emitting them from a projection nobody could re-confirm would let a directory
outage silently extend access — the same failure the reconciler refuses with
``StaleProjectionError``. The staleness is still reported in attributes, because
policy needs to see the degradation to act on it: withhold the grant, not the
fact.

Direction of dependency is deliberate — this extension imports the governance
core, never the reverse (ADR-052).
"""

from __future__ import annotations

from typing import Any, Mapping

from axiom.extensions.builtins.directory.reconcile import (
    GROUP_PREFIX,
    MEMBER_RELATION,
    USER_PREFIX,
)
from axiom.extensions.builtins.directory.resolution import Membership
from axiom.governance.subject import SubjectContext


def subject_from_membership(
    membership: Membership,
    *,
    tenant: str | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> SubjectContext:
    """Project a resolved membership onto what the substrate ``Check`` consumes.

    ``attributes`` merges caller-supplied request attributes (on-campus, MFA,
    …) under the membership facts, which win on conflict so provenance cannot be
    spoofed by a caller.
    """
    extra = dict(attributes or {})
    extra.update(
        {
            "roles": tuple(membership.roles),
            "groups": tuple(g.id for g in membership.groups),
            "as_of": membership.as_of,
            "membership_stale": membership.stale,
            "membership_source": membership.source,
        }
    )

    tuples: tuple[tuple[str, str, str], ...] = ()
    if not membership.stale:
        tuples = tuple(
            (
                f"{USER_PREFIX}{membership.principal.subject}",
                MEMBER_RELATION,
                f"{GROUP_PREFIX}{g.id}",
            )
            for g in membership.groups
        )

    return SubjectContext(
        tenant=tenant,
        fga_user=f"{USER_PREFIX}{membership.principal.subject}",
        attributes=extra,
        contextual_tuples=tuples,
    )


__all__ = ["subject_from_membership"]
