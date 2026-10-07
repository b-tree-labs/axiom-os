# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Cross-root bridge: two independent federations joined by one bridge node.

Scenario (5 nodes, ADR-023 §1 "cross-sector bridge" + §3 "cross-bridge"):
  Federation A: a-root, a-leaf
  Federation B: b-root, b-leaf
  Bridge: a member of BOTH federations via two distinct identity-bound
  peer entries.

  1. Start all 5 nodes; init identity on each.
  2. Form federation A: a-leaf adds a-root.
  3. Form federation B: b-leaf adds b-root.
  4. bridge adds a-root AND b-root — two distinct Ed25519 identities
     bound on the bridge.
  5. Validate:
     - bridge's registry shows exactly 2 verified peers (one per
       federation), with different node_ids and fingerprints.
     - a-leaf cannot see b-root or b-leaf — no transitive reach across
       the bridge.
     - b-leaf cannot see a-root or a-leaf — symmetric isolation.
     - a-root and b-root each have bridge as a valid entry (bridge
       reached out to them) but NOT each other.

The bridge is a deterministic chokepoint. Any data crossing from A to B
must be an explicit bridge operation — out of scope here; we validate
only the topology shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.federation_lifecycle.harness import FederationHarness, docker_available

pytestmark = pytest.mark.federation_lifecycle

COMPOSE = Path(__file__).resolve().parent / "docker-compose.cross-root.yml"


def _nodes_list_names(fed: FederationHarness, node: str) -> set[str]:
    """Return display_names present in this node's `axi nodes list`."""
    entries = fed.exec_json(node, "axi nodes list --json")
    if isinstance(entries, dict):
        entries = entries.get("nodes", [])
    return {e.get("display_name") for e in entries if e.get("display_name")}


def _peer_entries(fed: FederationHarness, node: str) -> list[dict]:
    peers = fed.exec_json(node, "axi federation peers --json")
    if isinstance(peers, dict):
        peers = peers.get("peers", [])
    return peers


def test_cross_root_bridge_two_federations(request):
    ok, reason = docker_available()
    if not ok:
        pytest.skip(f"federation_lifecycle: {reason}")

    project = f"axifed_{request.node.name}".replace("[", "_").replace("]", "_").lower()
    all_nodes = ("a-root", "a-leaf", "b-root", "b-leaf", "bridge")

    with FederationHarness(
        project=project,
        compose_file=COMPOSE,
        nodes=all_nodes,
    ) as fed:
        fed.start()

        # 1. Init identity on every node.
        ids: dict[str, str] = {}
        for name in all_nodes:
            out = fed.exec_json(
                name,
                f"axi federation init --owner test@{name}.local --name {name} --json",
            )
            assert out["initialized"] is True
            ids[name] = fed.exec_json(name, "axi federation status --json")["node_id"]
        assert len(set(ids.values())) == len(all_nodes), (
            f"expected {len(all_nodes)} distinct node_ids, got {ids}"
        )

        # 2. Federation A: a-leaf adds a-root.
        result = fed.add_peer(from_node="a-leaf", to_node="a-root")
        assert result.get("identity_bound") is True, f"a-leaf→a-root failed: {result}"
        assert result.get("node_id") == ids["a-root"]

        # 3. Federation B: b-leaf adds b-root.
        result = fed.add_peer(from_node="b-leaf", to_node="b-root")
        assert result.get("identity_bound") is True, f"b-leaf→b-root failed: {result}"
        assert result.get("node_id") == ids["b-root"]

        # 4. bridge joins BOTH federations.
        for root in ("a-root", "b-root"):
            result = fed.add_peer(from_node="bridge", to_node=root)
            assert result.get("identity_bound") is True, (
                f"bridge→{root} identity binding failed: {result}"
            )
            assert result.get("node_id") == ids[root]

        fed.assert_federated("bridge", "a-root")
        fed.assert_federated("bridge", "b-root")

        # 5a. bridge has exactly 2 verified peers, across 2 identities.
        bridge_peers = _peer_entries(fed, "bridge")
        verified = [p for p in bridge_peers if p.get("state") == "verified"]
        verified_names = {p.get("display_name") for p in verified}
        assert verified_names == {"a-root", "b-root"}, (
            f"bridge should have exactly {{a-root, b-root}} verified, got {verified_names}"
        )
        # Distinct Ed25519 identities (node_ids must differ).
        a_entry = next(p for p in verified if p.get("display_name") == "a-root")
        b_entry = next(p for p in verified if p.get("display_name") == "b-root")
        assert a_entry.get("node_id") != b_entry.get("node_id"), (
            "bridge's two peer entries share a node_id — federations would be "
            f"indistinguishable. a={a_entry}, b={b_entry}"
        )
        assert a_entry.get("node_id") == ids["a-root"]
        assert b_entry.get("node_id") == ids["b-root"]

        # 5b. a-leaf cannot see federation B at all — no transitive reach.
        a_leaf_view = _nodes_list_names(fed, "a-leaf")
        for forbidden in ("b-root", "b-leaf"):
            assert forbidden not in a_leaf_view, (
                f"a-leaf unexpectedly sees {forbidden}; transitive reach across "
                f"bridge detected. nodes_list={a_leaf_view}"
            )

        # 5c. b-leaf cannot see federation A.
        b_leaf_view = _nodes_list_names(fed, "b-leaf")
        for forbidden in ("a-root", "a-leaf"):
            assert forbidden not in b_leaf_view, (
                f"b-leaf unexpectedly sees {forbidden}; transitive reach across "
                f"bridge detected. nodes_list={b_leaf_view}"
            )

        # 5d. a-root and b-root have no knowledge of each other.
        a_root_view = _nodes_list_names(fed, "a-root")
        b_root_view = _nodes_list_names(fed, "b-root")
        assert "b-root" not in a_root_view and "b-leaf" not in a_root_view, (
            f"a-root leaked federation B peers: {a_root_view}"
        )
        assert "a-root" not in b_root_view and "a-leaf" not in b_root_view, (
            f"b-root leaked federation A peers: {b_root_view}"
        )
