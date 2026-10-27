# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Trust-weighted multi-source retrieval.

When :class:`~axiom.memory.composition.CompositionService` returns
fragments that originated on different nodes in the federation, the
synthesizer needs to weight each fragment by *who* asserted it. This
module is the bridge: take a list of :class:`MemoryFragment`, attach
the originating node's trust score, and return a stable, sorted
sequence ready for prompt synthesis.

Design notes:

- **Reuse the existing TrustGraph (ADR-028).** This module does no
  scoring math — it routes lookups through
  :meth:`TrustGraph.resolve` so every change to the optimistic-with-
  adaptation model upstream automatically applies here.
- **Local-origin fragments score 1.0.** A fragment your own node
  produced is, by definition, as trusted as you are. ``trust_basis``
  reads ``"local-origin"``.
- **Verified-peer fragments resolve through the trust graph.** First
  match for an explicit (trustor → principal) endorsement; otherwise
  the context's admission threshold is the optimistic TOFU baseline.
- **Unverified-origin fragments score 0.0** with
  ``trust_basis="unverified"``. The chat agent should still surface
  them but flag them as "claims from an unidentified source".
- **Deterministic ordering.** Output is sorted by
  ``(-trust_score, fragment.id)`` so prompt assembly is reproducible.
- **Domain-agnostic.** No nuclear / classroom / Prague vocabulary.
  The principal naming uses the platform-wide ``@name:context``
  convention.

The companion in ``axiom.extensions.builtins.memory.trust_retrieval_api``
exposes this primitive as the MCP tools ``axiom_memory__retrieve``
(decorated wrapper) and ``axiom_trust__node_score(node_id)``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from axiom.memory.trust import TrustContext, TrustGraph

if TYPE_CHECKING:
    from axiom.memory.fragment import MemoryFragment


# ---------------------------------------------------------------------------
# Peer registry view — the minimal facet we need for trust lookups
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PeerRegistryView:
    """Minimal read-only projection of the peer registry.

    The full ``NodeRegistry`` (federation/discovery.py) carries
    transport details we don't need here. This dataclass is the
    explicit contract — callers can build it from any registry shape
    without a hard dependency on the federation module.

    Attributes
    ----------
    local_node_id : str
        Cryptographic node id of *this* machine. Fragments authored
        by ``local_principals`` map to this node and earn ``1.0``.
    local_principals : frozenset[str]
        Principal ids that act on this node (typically just the
        node's owner; agents authoring on the node also count).
    verified_peer_principals : dict[str, str]
        Map of ``principal_id`` → ``node_id`` for peers we have
        cryptographically verified (see
        :class:`NodeState.VERIFIED`). A principal whose origin node
        is *not* in this map scores 0.0 (unverified).
    verified_node_ids : frozenset[str]
        All node ids that have completed identity binding. Used by
        :func:`trust_score_for_node` to answer "do we even know
        about this node?" without needing the principal map.
    """

    local_node_id: str
    local_principals: frozenset[str] = field(default_factory=frozenset)
    verified_peer_principals: dict[str, str] = field(default_factory=dict)
    verified_node_ids: frozenset[str] = field(default_factory=frozenset)


# ---------------------------------------------------------------------------
# TrustWeightedFragment — what callers consume
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrustWeightedFragment:
    """A fragment plus its source-derived trust score.

    The fragment itself is unchanged (immutable), so this is a
    decoration, not a mutation.

    Attributes
    ----------
    fragment : MemoryFragment
        The original fragment, untouched.
    trust_score : float
        Score in ``[0.0, 1.0]``. ``1.0`` for local-origin, the
        TrustGraph-resolved score for verified peers, ``0.0`` for
        unverified origins.
    trust_basis : str
        Short tag explaining *why* the score landed there:

        - ``"local-origin"`` — fragment authored on this node
        - ``"explicit-endorsement"`` — graph carried a direct
          ``TrustRecord`` from the trustor for this principal
        - ``"verified-peer-tofu"`` — verified node, no explicit
          record, fell back to ``context.admission_threshold``
        - ``"unverified"`` — origin node is not in the registry
    source_node_id : str
        Originating node id (empty for unverified origins).
    trust_path : tuple[str, ...]
        The chain of principals consulted to arrive at the score.
        For local: ``(local_principal,)``. For explicit: ``(trustor,
        target_principal)``. For TOFU: ``(trustor, target_principal,
        "tofu")``. For unverified: ``()``. The chat agent can render
        this as "<peer-name> (verified, endorsed by you)" etc.
    """

    fragment: "MemoryFragment"
    trust_score: float
    trust_basis: str
    source_node_id: str
    trust_path: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        """JSON-safe projection. The MCP tool layer emits this shape."""
        return {
            "fragment_id": self.fragment.id,
            "principal_id": self.fragment.provenance.principal_id,
            "trust_score": self.trust_score,
            "trust_basis": self.trust_basis,
            "source_node_id": self.source_node_id,
            "trust_path": list(self.trust_path),
            "summary": self.fragment.content.get("summary", ""),
            "fact_kind": self.fragment.content.get("fact_kind", ""),
        }


# ---------------------------------------------------------------------------
# trust_score_for_node — explicit single-node query
# ---------------------------------------------------------------------------


def trust_score_for_node(
    *,
    node_id: str,
    local_node_id: str,
    peer_view: PeerRegistryView,
    trust_graph: TrustGraph,
    trust_context: TrustContext,
    trustor: str,
) -> tuple[float, str]:
    """Resolve the trust score of a node by id.

    Returns ``(score, basis)``. Local resolves to ``(1.0,
    "local-origin")``; verified peers resolve through the trust
    graph; unknown nodes resolve to ``(0.0, "unverified")``.

    Powers the ``axiom_trust__node_score`` MCP tool.
    """
    if node_id == local_node_id:
        return 1.0, "local-origin"

    if node_id not in peer_view.verified_node_ids:
        return 0.0, "unverified"

    # Find the principal(s) that author on this peer node, then ask
    # the trust graph. We pick the highest-scoring principal so a
    # peer's most-trusted persona dominates the node's score.
    candidates = [
        principal
        for principal, nid in peer_view.verified_peer_principals.items()
        if nid == node_id
    ]
    if not candidates:
        # Verified node id with no associated principals — fall back
        # to the admission threshold so the peer isn't punished for
        # an empty registry projection.
        return trust_context.admission_threshold, "verified-peer-no-principals"

    best_score = -1.0
    best_basis = "verified-peer-tofu"
    for principal in candidates:
        score = trust_graph.resolve(
            trustor=trustor, subject=principal, context=trust_context
        )
        # Did the explicit-record branch win? resolve() doesn't tell
        # us, so we re-check direct records.
        is_explicit = any(
            r.trustor == trustor
            and r.target.context == trust_context.id
            and r.target.principal == principal
            for r in trust_graph.records
        )
        basis = "explicit-endorsement" if is_explicit else "verified-peer-tofu"
        if score > best_score:
            best_score = score
            best_basis = basis
    return best_score, best_basis


# ---------------------------------------------------------------------------
# decorate_fragments_with_trust — the multi-source retrieval entry point
# ---------------------------------------------------------------------------


def decorate_fragments_with_trust(
    fragments: list["MemoryFragment"],
    *,
    local_node_id: str,
    peer_view: PeerRegistryView,
    trust_graph: TrustGraph,
    trust_context: TrustContext,
    trustor: str,
) -> list[TrustWeightedFragment]:
    """Attach a trust score + basis to every fragment, sort, return.

    Sorting is ``(-trust_score, fragment.id)`` for deterministic
    prompt synthesis: highest-trust first, lexicographic id ties
    break stably.

    Parameters
    ----------
    fragments : list[MemoryFragment]
        Raw fragments from a multi-source retrieve (own node + any
        federated pulls). The list is treated as immutable.
    local_node_id : str
        Cryptographic node id of this machine. Fragments authored
        by a principal in ``peer_view.local_principals`` are scored
        ``1.0``.
    peer_view : PeerRegistryView
        Read-only projection of the peer registry — see the class.
    trust_graph : TrustGraph
        The trust graph (ADR-028) consulted for verified peers. We
        do NOT reimplement scoring; we only ask
        :meth:`TrustGraph.resolve`.
    trust_context : TrustContext
        Domain × maturity × classification bundle. Default for
        federation use is ``admission_threshold=0.5`` (the value
        TOFU peers earn before any explicit endorsement).
    trustor : str
        Principal whose perspective the trust graph is queried from.
        Typically the local node's primary human principal.

    Returns
    -------
    list[TrustWeightedFragment]
        Decorated, sorted fragments. The fragment objects themselves
        are unchanged.
    """
    decorated: list[TrustWeightedFragment] = []
    for frag in fragments:
        principal = frag.provenance.principal_id
        if principal in peer_view.local_principals:
            decorated.append(
                TrustWeightedFragment(
                    fragment=frag,
                    trust_score=1.0,
                    trust_basis="local-origin",
                    source_node_id=local_node_id,
                    trust_path=(principal,),
                )
            )
            continue

        if principal in peer_view.verified_peer_principals:
            node_id = peer_view.verified_peer_principals[principal]
            score, basis = trust_score_for_node(
                node_id=node_id,
                local_node_id=local_node_id,
                peer_view=peer_view,
                trust_graph=trust_graph,
                trust_context=trust_context,
                trustor=trustor,
            )
            if basis == "explicit-endorsement":
                path = (trustor, principal)
            else:
                path = (trustor, principal, "tofu")
            decorated.append(
                TrustWeightedFragment(
                    fragment=frag,
                    trust_score=score,
                    trust_basis=basis,
                    source_node_id=node_id,
                    trust_path=path,
                )
            )
            continue

        # Unverified principal — not on local, not in the verified
        # peer map. Surface but flag.
        decorated.append(
            TrustWeightedFragment(
                fragment=frag,
                trust_score=0.0,
                trust_basis="unverified",
                source_node_id="",
                trust_path=(),
            )
        )

    decorated.sort(key=lambda d: (-d.trust_score, d.fragment.id))
    return decorated
