# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Bipartite access graphs + retrospective access check.

Per Rezazadeh et al. 2025 (arXiv 2505.18279, Collaborative Memory
§3.1/§3.3): memory access is controlled by two time-varying
bipartite graphs:

    G_UA(t) ⊆ U × A   (users × agents)
    G_AR(t) ⊆ A × R   (agents × resources)

A fragment `m` with immutable provenance `(T, U(m), A(m), R(m))` is
visible to user `u` through querying agent `a` at time `t` iff:

    A(m) ⊆ A(u,t)                            (all contributing agents reachable)
    R(m) ⊆ R(a,t)                            (all touched resources reachable)
    a ∈ A(u,t)                               (querying agent reachable)

Revocation = edge removal. Fragments never mutate.

Federation extends this (task #16, already built): a third bipartite
U↔Node / Node↔Agent layer routes through the trust chain. The
current module handles the local-node case; federation composes on
top.

Pure functional API — every transition returns a new AccessGraphs.
Persistence is the caller's concern (DB-backed store in #41 extends
this).
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .fragment import MemoryFragment


# ---------------------------------------------------------------------------
# AccessGraphs data model
# ---------------------------------------------------------------------------


@dataclass
class AccessGraphs:
    """Two bipartite edge sets representing G_UA and G_AR.

    Edges stored as frozensets of (left, right) tuples for fast set
    membership and clean immutability semantics across transitions.
    """

    user_agent: set[tuple[str, str]] = field(default_factory=set)
    agent_resource: set[tuple[str, str]] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Edge mutations (return new graphs)
# ---------------------------------------------------------------------------


def add_user_agent_edge(
    graphs: AccessGraphs, user: str, agent: str
) -> AccessGraphs:
    g = deepcopy(graphs)
    g.user_agent.add((user, agent))
    return g


def remove_user_agent_edge(
    graphs: AccessGraphs, user: str, agent: str
) -> AccessGraphs:
    g = deepcopy(graphs)
    g.user_agent.discard((user, agent))
    return g


def add_agent_resource_edge(
    graphs: AccessGraphs, agent: str, resource: str
) -> AccessGraphs:
    g = deepcopy(graphs)
    g.agent_resource.add((agent, resource))
    return g


def remove_agent_resource_edge(
    graphs: AccessGraphs, agent: str, resource: str
) -> AccessGraphs:
    g = deepcopy(graphs)
    g.agent_resource.discard((agent, resource))
    return g


# ---------------------------------------------------------------------------
# Accessibility views
# ---------------------------------------------------------------------------


def agents_for_user(graphs: AccessGraphs, user: str) -> frozenset[str]:
    """A(u,t): set of agents the user can reach at the current time."""
    return frozenset(a for u, a in graphs.user_agent if u == user)


def resources_for_agent(graphs: AccessGraphs, agent: str) -> frozenset[str]:
    """R(a,t): set of resources the agent can reach at the current time."""
    return frozenset(r for a_, r in graphs.agent_resource if a_ == agent)


# ---------------------------------------------------------------------------
# Ownership base case (ADR-026) — a master reads their own memory
# ---------------------------------------------------------------------------


def _owns_fragment(user: str, fragment: MemoryFragment) -> bool:
    """True iff ``user`` is the fragment's owner (ADR-026 read base case).

    Ownership is authoritative when set: the requesting principal owns the
    fragment iff they are its ownership ``master``. For a fragment with no
    ownership record — anonymously-owned per ADR-026's migration note — the
    contributing principal (``provenance.principal_id``) is the de-facto
    owner, so a memory authored by the user's own agent on their behalf
    resolves to them. This mirrors ``CompositionService.forget``'s
    ``frag.ownership or new_ownership(master=principal_id)`` fallback.

    Owner resolution is deliberately narrow: when ownership *is* set it is the
    only source of truth (a post-transfer clean break, ADR-026, hands the new
    master control — the original contributor no longer owns it), and an empty
    ``user`` (unresolved requester) owns nothing.
    """
    if not user:
        return False
    own = fragment.ownership
    if own is not None:
        return own.master == user
    return fragment.provenance.principal_id == user


# ---------------------------------------------------------------------------
# Retrospective access check (the paper's core operator)
# ---------------------------------------------------------------------------


def is_visible(
    graphs: AccessGraphs,
    user: str,
    agent: str,
    fragment: MemoryFragment,
) -> bool:
    """Retrospective access check per §3.3, with the ADR-026 ownership base case.

    Read is granted when EITHER:

    - **(ADR-026 base case)** the requesting principal OWNS the fragment — they
      are its ownership master (or, for an anonymously-owned fragment, its
      contributing principal). The owner's read right is *intrinsic*, not
      graph-derived: the access GRAPH governs peer / delegated *cross-principal*
      access, never a principal reading their own memory. Without this, an empty
      access graph drops even the user's OWN fragment as read-denied before any
      downstream gate runs (OQ-A2-1). This grants the read right ONLY — it does
      NOT serve: vault/secret/cross-account/tier decisions live at the serving
      gate (ADR-087 D7), which runs after read() and is unchanged.

    OR, *at the current state of the graphs* (the paper's §3.3 check, applied
    unchanged to every NON-owner / cross-principal read):

    - The querying agent is itself reachable by the user.
    - Every agent that contributed to the fragment is reachable by the user.
    - Every resource the fragment touched is reachable by the querying agent.

    Revocation happens by removing an edge; the fragment's stored provenance
    never changes. The base case is additive and narrow — a non-owner falls
    straight through to the graph check below, so no cross-principal path opens.
    """
    # ADR-026 ownership base case — the master reads their own memory
    # regardless of access-graph population. Non-owners fall through unchanged.
    if _owns_fragment(user, fragment):
        return True

    u_agents = agents_for_user(graphs, user)

    # The querying agent must itself be accessible to the user.
    if agent not in u_agents:
        return False

    # Every contributing agent must be in A(u,t).
    if not fragment.provenance.agents.issubset(u_agents):
        return False

    # Every touched resource must be in R(a,t).
    a_resources = resources_for_agent(graphs, agent)
    if not fragment.provenance.resources.issubset(a_resources):
        return False

    return True


def visible_fragments(
    graphs: AccessGraphs,
    user: str,
    agent: str,
    fragments: list[MemoryFragment],
) -> list[MemoryFragment]:
    """Filter a list of fragments to those currently visible.

    The paper's `M(u, a, t)` operator realized as a list filter.
    """
    return [f for f in fragments if is_visible(graphs, user, agent, f)]
