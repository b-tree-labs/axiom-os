# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""MCP-style API surface for trust-weighted retrieval.

Exposes two callable tools:

- ``axiom_memory__retrieve(...)`` — retrieve fragments by id and
  decorate each with the originating node's trust score.
- ``axiom_trust__node_score(node_id, ...)`` — explicit trust query
  for a single node.

Both functions return JSON-safe ``dict`` payloads ready to flow over
the MCP transport. They take an explicit
:class:`PeerRegistryView`, :class:`TrustGraph`, and
:class:`TrustContext` so they remain pure (no global state) and
trivially testable. The ``axi`` CLI / chat agent are responsible for
constructing those objects from the live registries before the call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from axiom.memory.trust import TrustContext, TrustGraph
from axiom.memory.trust_retrieval import (
    PeerRegistryView,
    decorate_fragments_with_trust,
    trust_score_for_node,
)

if TYPE_CHECKING:
    from axiom.memory.composition import CompositionService


# ---------------------------------------------------------------------------
# axiom_memory__retrieve
# ---------------------------------------------------------------------------


def axiom_memory__retrieve(
    *,
    composition: "CompositionService",
    fragment_ids: list[str],
    user: str,
    agent: str,
    local_node_id: str,
    peer_view: PeerRegistryView,
    trust_graph: TrustGraph,
    trust_context: TrustContext,
    trustor: str,
) -> dict:
    """Retrieve fragments and decorate each with its source's trust score.

    Returns
    -------
    dict
        Shape::

            {
              "fragments": [
                {
                  "fragment_id": str,
                  "principal_id": str,
                  "trust_score": float,
                  "trust_basis": str,
                  "source_node_id": str,
                  "trust_path": list[str],
                  "summary": str,
                  "fact_kind": str,
                },
                ...
              ],
              "trust_context": str,
              "trustor": str,
            }

        The list is sorted by ``(-trust_score, fragment_id)`` so the
        chat agent's synthesis prompt is deterministic across runs.
    """
    fragments = composition.read(
        fragment_ids=fragment_ids,
        user=user,
        agent=agent,
    )
    decorated = decorate_fragments_with_trust(
        fragments,
        local_node_id=local_node_id,
        peer_view=peer_view,
        trust_graph=trust_graph,
        trust_context=trust_context,
        trustor=trustor,
    )
    return {
        "fragments": [d.to_dict() for d in decorated],
        "trust_context": trust_context.id,
        "trustor": trustor,
    }


# ---------------------------------------------------------------------------
# axiom_trust__node_score
# ---------------------------------------------------------------------------


def axiom_trust__node_score(
    *,
    node_id: str,
    local_node_id: str,
    peer_view: PeerRegistryView,
    trust_graph: TrustGraph,
    trust_context: TrustContext,
    trustor: str,
) -> dict:
    """Explicit trust query for a single node.

    Returns
    -------
    dict
        Shape::

            {
              "node_id": str,
              "trust_score": float,
              "trust_basis": str,
              "trustor": str,
              "trust_context": str,
            }
    """
    score, basis = trust_score_for_node(
        node_id=node_id,
        local_node_id=local_node_id,
        peer_view=peer_view,
        trust_graph=trust_graph,
        trust_context=trust_context,
        trustor=trustor,
    )
    return {
        "node_id": node_id,
        "trust_score": score,
        "trust_basis": basis,
        "trustor": trustor,
        "trust_context": trust_context.id,
    }
