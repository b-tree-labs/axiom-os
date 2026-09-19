# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Tests for trust-weighted multi-source retrieval.

When ``axiom_memory__retrieve`` returns fragments from multiple peers,
each fragment is decorated with the originating node's trust score.
The chat agent uses these scores to weight synthesis: high-trust peers
get more weight; low-trust peers are surfaced but flagged.

Source-of-truth for scoring: the existing ``TrustGraph`` (ADR-028).
This module adds ZERO new scoring math — it only routes lookups.

Per the asymmetric-edge demo brief, the four invariants under test:

1. Local-origin fragments score 1.0 (own node).
2. Verified-peer fragments score per the trust graph (TOFU =
   ``context.admission_threshold`` (0.5 in this demo's context);
   explicit endorsement bumps it).
3. Unverified-peer fragments score 0.0 with ``trust_basis="unverified"``.
4. Multi-fragment retrieval emits scores in deterministic order so
   downstream synthesis prompts are reproducible.
"""

from __future__ import annotations

import pytest

from axiom.memory.fragment import (
    CognitiveType,
    create_fragment,
)
from axiom.memory.ownership import TrustTarget
from axiom.memory.trust import (
    TrustContext,
    TrustGraph,
    TrustRecord,
)
from axiom.memory.trust_retrieval import (
    PeerRegistryView,
    TrustWeightedFragment,
    decorate_fragments_with_trust,
    trust_score_for_node,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


LOCAL_NODE_ID = "local-node-aaa"
PEER_NODE_ID = "peer-node-bbb"
THIRD_NODE_ID = "third-node-ccc"
STRANGER_NODE_ID = "stranger-node-zzz"

LOCAL_PRINCIPAL = "@ben:example-org"
PEER_PRINCIPAL = "agent:peer"
THIRD_PRINCIPAL = "agent:edge-laptop"
STRANGER_PRINCIPAL = "@unknown:elsewhere"


def _make_fragment(principal_id: str, summary: str):
    """Build a minimal semantic fragment owned by ``principal_id``."""
    return create_fragment(
        content={
            "fact_kind": "claim",
            "summary": summary,
        },
        cognitive_type=CognitiveType.SEMANTIC.value,
        principal_id=principal_id,
        agents=set(),
        resources=set(),
    )


def _make_peer_view(
    *,
    verified: dict[str, str] | None = None,
    unverified: set[str] | None = None,
) -> PeerRegistryView:
    """Build a tiny in-memory peer registry view for tests.

    ``verified``: node_id -> principal_id of the originator on that node.
    ``unverified``: a set of principal_ids whose origin node is unknown.
    """
    return PeerRegistryView(
        local_node_id=LOCAL_NODE_ID,
        local_principals=frozenset({LOCAL_PRINCIPAL}),
        verified_peer_principals={
            principal: node_id
            for node_id, principal in (verified or {}).items()
        },
        verified_node_ids=frozenset((verified or {}).keys()),
    )


# ---------------------------------------------------------------------------
# trust_score_for_node — the explicit single-node lookup
# ---------------------------------------------------------------------------


class TestTrustScoreForNode:
    def test_local_node_scores_one(self) -> None:
        view = _make_peer_view()
        ctx = TrustContext(id="federation", admission_threshold=0.5)
        score, basis = trust_score_for_node(
            node_id=LOCAL_NODE_ID,
            local_node_id=LOCAL_NODE_ID,
            peer_view=view,
            trust_graph=TrustGraph(),
            trust_context=ctx,
            trustor=LOCAL_PRINCIPAL,
        )
        assert score == 1.0
        assert basis.startswith("local")

    def test_verified_peer_with_no_explicit_record_scores_at_admission_default(self) -> None:
        view = _make_peer_view(verified={PEER_NODE_ID: PEER_PRINCIPAL})
        ctx = TrustContext(id="federation", admission_threshold=0.5)
        score, basis = trust_score_for_node(
            node_id=PEER_NODE_ID,
            local_node_id=LOCAL_NODE_ID,
            peer_view=view,
            trust_graph=TrustGraph(),
            trust_context=ctx,
            trustor=LOCAL_PRINCIPAL,
        )
        assert score == 0.5
        assert "tofu" in basis.lower() or "verified-peer" in basis.lower()

    def test_explicit_endorsement_overrides_admission_default(self) -> None:
        view = _make_peer_view(verified={PEER_NODE_ID: PEER_PRINCIPAL})
        ctx = TrustContext(id="federation", admission_threshold=0.5)
        graph = TrustGraph().with_record(
            TrustRecord(
                trustor=LOCAL_PRINCIPAL,
                target=TrustTarget(
                    principal=PEER_PRINCIPAL,
                    role=None,
                    context="federation",
                ),
                score=0.9,
            )
        )
        score, basis = trust_score_for_node(
            node_id=PEER_NODE_ID,
            local_node_id=LOCAL_NODE_ID,
            peer_view=view,
            trust_graph=graph,
            trust_context=ctx,
            trustor=LOCAL_PRINCIPAL,
        )
        assert score == 0.9
        assert "endorsed" in basis.lower() or "explicit" in basis.lower()

    def test_unverified_node_scores_zero(self) -> None:
        view = _make_peer_view()  # nothing verified
        ctx = TrustContext(id="federation", admission_threshold=0.5)
        score, basis = trust_score_for_node(
            node_id=STRANGER_NODE_ID,
            local_node_id=LOCAL_NODE_ID,
            peer_view=view,
            trust_graph=TrustGraph(),
            trust_context=ctx,
            trustor=LOCAL_PRINCIPAL,
        )
        assert score == 0.0
        assert basis == "unverified"


# ---------------------------------------------------------------------------
# decorate_fragments_with_trust — the multi-source retrieval entry point
# ---------------------------------------------------------------------------


class TestDecorateFragmentsWithTrust:
    def test_local_fragment_scores_one(self) -> None:
        view = _make_peer_view()
        ctx = TrustContext(id="federation", admission_threshold=0.5)
        frag = _make_fragment(LOCAL_PRINCIPAL, "local claim")
        decorated = decorate_fragments_with_trust(
            [frag],
            local_node_id=LOCAL_NODE_ID,
            peer_view=view,
            trust_graph=TrustGraph(),
            trust_context=ctx,
            trustor=LOCAL_PRINCIPAL,
        )
        assert len(decorated) == 1
        assert decorated[0].trust_score == 1.0
        assert decorated[0].trust_basis.startswith("local")
        assert decorated[0].source_node_id == LOCAL_NODE_ID

    def test_verified_peer_fragment_uses_trust_graph(self) -> None:
        view = _make_peer_view(verified={PEER_NODE_ID: PEER_PRINCIPAL})
        ctx = TrustContext(id="federation", admission_threshold=0.5)
        frag = _make_fragment(PEER_PRINCIPAL, "peer claim")
        decorated = decorate_fragments_with_trust(
            [frag],
            local_node_id=LOCAL_NODE_ID,
            peer_view=view,
            trust_graph=TrustGraph(),
            trust_context=ctx,
            trustor=LOCAL_PRINCIPAL,
        )
        assert decorated[0].trust_score == 0.5
        assert decorated[0].source_node_id == PEER_NODE_ID

    def test_unverified_peer_fragment_scores_zero_with_unverified_basis(self) -> None:
        view = _make_peer_view()  # nothing verified
        ctx = TrustContext(id="federation", admission_threshold=0.5)
        frag = _make_fragment(STRANGER_PRINCIPAL, "stranger claim")
        decorated = decorate_fragments_with_trust(
            [frag],
            local_node_id=LOCAL_NODE_ID,
            peer_view=view,
            trust_graph=TrustGraph(),
            trust_context=ctx,
            trustor=LOCAL_PRINCIPAL,
        )
        assert decorated[0].trust_score == 0.0
        assert decorated[0].trust_basis == "unverified"
        assert decorated[0].source_node_id == ""

    def test_multi_fragment_order_is_deterministic(self) -> None:
        """Same inputs → same output order, every time.

        Sorted by (-trust_score, fragment.id) so the highest-trust
        fragment always lands first and ties break stably.
        """
        view = _make_peer_view(
            verified={
                PEER_NODE_ID: PEER_PRINCIPAL,
                THIRD_NODE_ID: THIRD_PRINCIPAL,
            }
        )
        ctx = TrustContext(id="federation", admission_threshold=0.5)
        graph = TrustGraph().with_record(
            TrustRecord(
                trustor=LOCAL_PRINCIPAL,
                target=TrustTarget(
                    principal=PEER_PRINCIPAL,
                    role=None,
                    context="federation",
                ),
                score=0.9,
            )
        )
        frags = [
            _make_fragment(THIRD_PRINCIPAL, "third claim"),     # 0.5
            _make_fragment(STRANGER_PRINCIPAL, "stranger"),     # 0.0
            _make_fragment(LOCAL_PRINCIPAL, "local claim"),     # 1.0
            _make_fragment(PEER_PRINCIPAL, "peer claim"),   # 0.9
        ]
        decorated = decorate_fragments_with_trust(
            frags,
            local_node_id=LOCAL_NODE_ID,
            peer_view=view,
            trust_graph=graph,
            trust_context=ctx,
            trustor=LOCAL_PRINCIPAL,
        )
        scores = [d.trust_score for d in decorated]
        assert scores == [1.0, 0.9, 0.5, 0.0]

        # Determinism: same inputs → identical second pass.
        again = decorate_fragments_with_trust(
            frags,
            local_node_id=LOCAL_NODE_ID,
            peer_view=view,
            trust_graph=graph,
            trust_context=ctx,
            trustor=LOCAL_PRINCIPAL,
        )
        assert [d.fragment.id for d in decorated] == [d.fragment.id for d in again]

    def test_decorated_payload_carries_principal_and_node(self) -> None:
        view = _make_peer_view(verified={PEER_NODE_ID: PEER_PRINCIPAL})
        ctx = TrustContext(id="federation", admission_threshold=0.5)
        frag = _make_fragment(PEER_PRINCIPAL, "peer claim")
        decorated = decorate_fragments_with_trust(
            [frag],
            local_node_id=LOCAL_NODE_ID,
            peer_view=view,
            trust_graph=TrustGraph(),
            trust_context=ctx,
            trustor=LOCAL_PRINCIPAL,
        )[0]
        payload = decorated.to_dict()
        assert payload["trust_score"] == 0.5
        assert "trust_basis" in payload
        assert payload["source_node_id"] == PEER_NODE_ID
        assert payload["principal_id"] == PEER_PRINCIPAL
        assert payload["fragment_id"] == frag.id


# ---------------------------------------------------------------------------
# Module-level invariants
# ---------------------------------------------------------------------------


class TestModuleInvariants:
    def test_decorated_fragments_are_immutable_dataclass(self) -> None:
        view = _make_peer_view()
        ctx = TrustContext(id="federation", admission_threshold=0.5)
        frag = _make_fragment(LOCAL_PRINCIPAL, "local claim")
        decorated = decorate_fragments_with_trust(
            [frag],
            local_node_id=LOCAL_NODE_ID,
            peer_view=view,
            trust_graph=TrustGraph(),
            trust_context=ctx,
            trustor=LOCAL_PRINCIPAL,
        )[0]
        assert isinstance(decorated, TrustWeightedFragment)
        with pytest.raises(Exception):
            decorated.trust_score = 0.0  # type: ignore[misc]

    def test_empty_input_returns_empty_list(self) -> None:
        view = _make_peer_view()
        ctx = TrustContext(id="federation", admission_threshold=0.5)
        out = decorate_fragments_with_trust(
            [],
            local_node_id=LOCAL_NODE_ID,
            peer_view=view,
            trust_graph=TrustGraph(),
            trust_context=ctx,
            trustor=LOCAL_PRINCIPAL,
        )
        assert out == []


# ---------------------------------------------------------------------------
# MCP-style API surface tests
# ---------------------------------------------------------------------------


class _StubComposition:
    """Tiny CompositionService stand-in: ``read(ids)`` returns fragments by id."""

    def __init__(self, fragments):
        self._by_id = {f.id: f for f in fragments}

    def read(self, fragment_ids, user, agent, at=None):
        return [self._by_id[fid] for fid in fragment_ids if fid in self._by_id]


class TestAxiomMemoryRetrieveTool:
    def test_returns_jsonable_payload_with_decorated_fragments(self) -> None:
        from axiom.extensions.builtins.memory.trust_retrieval_api import (
            axiom_memory__retrieve,
        )

        view = _make_peer_view(verified={PEER_NODE_ID: PEER_PRINCIPAL})
        ctx = TrustContext(id="federation", admission_threshold=0.5)
        local = _make_fragment(LOCAL_PRINCIPAL, "local claim")
        peer_frag = _make_fragment(PEER_PRINCIPAL, "peer claim")
        comp = _StubComposition([local, peer_frag])

        payload = axiom_memory__retrieve(
            composition=comp,
            fragment_ids=[local.id, peer_frag.id],
            user=LOCAL_PRINCIPAL,
            agent="agent:walle",
            local_node_id=LOCAL_NODE_ID,
            peer_view=view,
            trust_graph=TrustGraph(),
            trust_context=ctx,
            trustor=LOCAL_PRINCIPAL,
        )
        assert payload["trust_context"] == "federation"
        assert payload["trustor"] == LOCAL_PRINCIPAL
        assert len(payload["fragments"]) == 2
        # Sorted highest-trust-first.
        assert payload["fragments"][0]["trust_score"] == 1.0
        assert payload["fragments"][1]["trust_score"] == 0.5


class TestAxiomTrustNodeScoreTool:
    def test_returns_score_and_basis(self) -> None:
        from axiom.extensions.builtins.memory.trust_retrieval_api import (
            axiom_trust__node_score,
        )

        view = _make_peer_view(verified={PEER_NODE_ID: PEER_PRINCIPAL})
        ctx = TrustContext(id="federation", admission_threshold=0.5)
        out = axiom_trust__node_score(
            node_id=PEER_NODE_ID,
            local_node_id=LOCAL_NODE_ID,
            peer_view=view,
            trust_graph=TrustGraph(),
            trust_context=ctx,
            trustor=LOCAL_PRINCIPAL,
        )
        assert out["node_id"] == PEER_NODE_ID
        assert out["trust_score"] == 0.5
        assert out["trust_basis"] in ("verified-peer-tofu",)
        assert out["trustor"] == LOCAL_PRINCIPAL
        assert out["trust_context"] == "federation"

    def test_unknown_node_scores_zero_unverified(self) -> None:
        from axiom.extensions.builtins.memory.trust_retrieval_api import (
            axiom_trust__node_score,
        )

        view = _make_peer_view()
        ctx = TrustContext(id="federation", admission_threshold=0.5)
        out = axiom_trust__node_score(
            node_id=STRANGER_NODE_ID,
            local_node_id=LOCAL_NODE_ID,
            peer_view=view,
            trust_graph=TrustGraph(),
            trust_context=ctx,
            trustor=LOCAL_PRINCIPAL,
        )
        assert out["trust_score"] == 0.0
        assert out["trust_basis"] == "unverified"

    def test_local_node_scores_one(self) -> None:
        from axiom.extensions.builtins.memory.trust_retrieval_api import (
            axiom_trust__node_score,
        )

        view = _make_peer_view()
        ctx = TrustContext(id="federation", admission_threshold=0.5)
        out = axiom_trust__node_score(
            node_id=LOCAL_NODE_ID,
            local_node_id=LOCAL_NODE_ID,
            peer_view=view,
            trust_graph=TrustGraph(),
            trust_context=ctx,
            trustor=LOCAL_PRINCIPAL,
        )
        assert out["trust_score"] == 1.0
        assert out["trust_basis"] == "local-origin"
