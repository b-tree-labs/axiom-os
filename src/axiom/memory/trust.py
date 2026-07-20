# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Trust graph (#47, ADR-028) — EigenTrust-style derived trust.

Per ADR-028 (as-implemented). Four core concepts:

1. **TrustRecord** — an explicit `(trustor, target, context, score)`
   assertion. Target is a `TrustTarget(principal, role, context)`
   from ADR-026 so role succession rebinds naturally.
2. **TrustContext** — a `(domain × maturity × classification)`
   bundle. Users tune α decay, admission threshold, blast radius
   *at this level* (not per-fragment). Default values follow the
   "expect good behavior, tighten with emergent bad behavior"
   principle: α=0.8, threshold=0.3, blast_radius=1.
3. **TrustGraph** — the collection of records + role-membership
   resolver + observation log.
4. **Adaptation loop** — passive observation events feed a proposal
   engine; humans review proposed demotions/promotions before
   applying.

Hierarchical resolution order (most → least specific):
  explicit-human > role > optimistic-default.

Derived trust via power iteration over the trust matrix (inspired
by EigenTrust, Kamvar et al. 2003). Each hop multiplies by α,
independent paths converge additively so proximity boosts trust.

Privacy: trust records are private to the trustor. Aggregate
derived scores may be shared with consent (future work).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from .ownership import TrustTarget

# ---------------------------------------------------------------------------
# TrustContext
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrustContext:
    """Tuning parameters for a trust domain.

    Defaults encode the "optimistic with adaptation" philosophy:
    low decay, low admission threshold, narrow blast radius.
    """

    id: str
    alpha_decay: float = 0.8
    admission_threshold: float = 0.3
    blast_radius_hops: int = 1


# ---------------------------------------------------------------------------
# TrustRecord
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrustRecord:
    """Explicit trust assertion. Private to the trustor."""

    trustor: str
    target: TrustTarget
    score: float                          # [0.0, 1.0]
    signature: bytes | None = None     # for cross-node transport


# ---------------------------------------------------------------------------
# ObservationEvent (adaptation-loop input)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservationEvent:
    """Passive observation of a principal's behavior.

    Kinds: breach_detected, peer_rejection, content_accepted,
    content_endorsed, etc. Weight lets callers express event
    magnitude (e.g., 0.3 for a soft peer-rejection, 1.0 for a
    hard policy breach).
    """

    observer: str
    subject: str
    kind: str
    weight: float
    at: str


# ---------------------------------------------------------------------------
# TrustGraph
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrustGraph:
    """The full trust state for a principal's view of the world.

    - `records`: direct TrustRecord assertions (private to trustor).
    - `role_membership`: role id → frozenset of principals in that role.
      Published by the owning org; consumed during hierarchical
      resolution. For this MVP, held in-memory; in production the
      federation layer publishes these as signed artifacts.
    - `observations`: adaptation-loop event log.
    """

    records: tuple[TrustRecord, ...] = ()
    role_membership: dict[str, frozenset[str]] = field(default_factory=dict)
    observations: tuple[ObservationEvent, ...] = ()

    # ------- Mutators (return new graph) -----------------------------------

    def with_record(self, record: TrustRecord) -> TrustGraph:
        return dataclasses.replace(
            self, records=tuple([*self.records, record])
        )

    def record_observation(self, event: ObservationEvent) -> TrustGraph:
        return dataclasses.replace(
            self, observations=tuple([*self.observations, event])
        )

    # ------- Queries -------------------------------------------------------

    def direct_score(
        self, trustor: str, subject: str, context: TrustContext
    ) -> float:
        """Explicit human-scoped record; falls to context default."""
        for r in self.records:
            if r.trustor != trustor:
                continue
            if r.target.context != context.id:
                continue
            if r.target.principal == subject:
                return r.score
        return context.admission_threshold

    def _role_score(
        self, trustor: str, subject: str, context: TrustContext
    ) -> float | None:
        """Role-scoped score (inherited via role membership)."""
        for r in self.records:
            if r.trustor != trustor:
                continue
            if r.target.context != context.id:
                continue
            if r.target.role is None:
                continue
            members = self.role_membership.get(r.target.role, frozenset())
            if subject in members:
                return r.score
        return None

    def resolve(
        self, trustor: str, subject: str, context: TrustContext
    ) -> float:
        """Hierarchical resolution: explicit-human > role > default."""
        # 1. Explicit human-scoped
        for r in self.records:
            if (r.trustor == trustor
                and r.target.context == context.id
                and r.target.principal == subject):
                return r.score
        # 2. Role-scoped via membership
        role_score = self._role_score(trustor, subject, context)
        if role_score is not None:
            return role_score
        # 3. Optimistic default
        return context.admission_threshold

    def records_visible_to(self, principal: str) -> tuple[TrustRecord, ...]:
        """Privacy: only the trustor sees their own records."""
        return tuple(r for r in self.records if r.trustor == principal)

    def observations_for(self, subject: str) -> tuple[ObservationEvent, ...]:
        return tuple(o for o in self.observations if o.subject == subject)

    # ------- Derived trust (power iteration) -------------------------------

    def derived_score(
        self, trustor: str, subject: str, context: TrustContext
    ) -> float:
        """EigenTrust-inspired power iteration over the trust matrix.

        Walk paths trustor → subject up to `blast_radius_hops` hops,
        multiplying by α per hop. Multiple paths contribute additively
        (capped at 1.0) — this is where proximity boosts come from.
        """
        # Build adjacency: principal → {target: score} filtered by context
        adj: dict[str, dict[str, float]] = {}
        for r in self.records:
            if r.target.context != context.id:
                continue
            if r.target.principal is None:
                continue
            adj.setdefault(r.trustor, {})[r.target.principal] = r.score

        # BFS up to blast_radius_hops, accumulating path contributions
        # Score contribution of a path = α^(hops-1) × product of edges
        # For simplicity we use α^(hop_count) × min-edge on the path
        max_hops = max(1, context.blast_radius_hops + 1)
        contributions: list[float] = []

        def walk(current: str, depth: int, path_min: float, visited: set):
            if depth > max_hops:
                return
            neighbors = adj.get(current, {})
            for nxt, edge_w in neighbors.items():
                if nxt in visited:
                    continue
                new_min = min(path_min, edge_w) if depth > 0 else edge_w
                if nxt == subject:
                    contribution = (context.alpha_decay ** depth) * new_min
                    contributions.append(contribution)
                else:
                    walk(nxt, depth + 1, new_min, visited | {nxt})

        walk(trustor, 0, 1.0, {trustor})

        # Additive combination (capped at 1.0) so proximity boosts
        if not contributions:
            return context.admission_threshold
        total = sum(contributions)
        return min(total, 1.0)


# ---------------------------------------------------------------------------
# Adaptation loop — propose adjustments from observations
# ---------------------------------------------------------------------------


def propose_adjustments(
    graph: TrustGraph,
    subject: str,
    breach_threshold: int = 3,
) -> list[dict]:
    """Examine observations for `subject` and propose trust adjustments.

    Simple rule set for MVP:
    - N breach_detected events within observation window → propose demotion.
    - N content_endorsed events → propose promotion.

    Real deployments tune thresholds + windows via the Karpathy loop
    CURIO runs. This function is pure — proposals are a list of dicts
    the caller routes to human review (per the "never auto-blacklist"
    principle).
    """
    observations = graph.observations_for(subject)
    breach_count = sum(
        1 for o in observations if o.kind == "breach_detected"
    )
    endorse_count = sum(
        1 for o in observations if o.kind == "content_endorsed"
    )

    proposals = []
    if breach_count >= breach_threshold:
        proposals.append({
            "subject": subject,
            "direction": "down",
            "reason": f"{breach_count} breach events observed",
            "suggested_delta": -0.2,
        })
    if endorse_count >= breach_threshold:
        proposals.append({
            "subject": subject,
            "direction": "up",
            "reason": f"{endorse_count} endorsement events observed",
            "suggested_delta": 0.1,
        })
    return proposals


# ---------------------------------------------------------------------------
# Role succession — rebind role-scoped records
# ---------------------------------------------------------------------------


def apply_succession(graph: TrustGraph, succession: dict) -> TrustGraph:
    """Apply a role succession to a trust graph.

    Role-scoped trust records auto-rebind via role_membership update
    (no record rewrites). Human-scoped records stay with the outgoing
    principal. This is the mechanism promised by ADR-026.
    """
    role = succession["role"]
    outgoing = succession["from"]
    incoming = succession["to"]

    new_membership = dict(graph.role_membership)
    current = set(new_membership.get(role, frozenset()))
    current.discard(outgoing)
    current.add(incoming)
    new_membership[role] = frozenset(current)

    return dataclasses.replace(graph, role_membership=new_membership)
