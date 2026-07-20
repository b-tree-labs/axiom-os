# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""3-level hierarchical federation: root → intermediate → leaves.

Scenario (4 nodes, ADR-023 §1 "hierarchical"):
  1. Start root + intermediate1 + leaf1a + leaf1b.
  2. `axi federation init` on each node (distinct Ed25519 keypairs).
  3. intermediate1 adds root as its peer (intermediate is a leaf relative
     to root).
  4. leaf1a and leaf1b each add intermediate1 as their peer. Leaves do
     NOT add root directly.
  5. Verify identity-bound peer sets per tier:
     - root: no peers (it was added *to*, but never added anyone itself)
     - intermediate1: exactly {root}
     - leaf1a, leaf1b: exactly {intermediate1}, and NOT root
  6. `axi federation peers --json` returns the expected set on each tier.
  7. Shut down intermediate1. Leaves lose visibility of the hub chain
     (any attempt to reach through intermediate would fail) but continue
     to operate locally (`axi federation status` still succeeds — self-
     sufficiency per ADR-016).

Validates that hierarchy is respected: peer graph is strictly parent-
child, not transitive, and that leaves survive an intermediate outage.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.federation_lifecycle.harness import FederationHarness, docker_available

pytestmark = pytest.mark.federation_lifecycle

COMPOSE = Path(__file__).resolve().parent / "docker-compose.hierarchical-3.yml"


def _peer_names(fed: FederationHarness, node: str) -> set[str]:
    """Return the set of display_names this node has as peers."""
    peers = fed.exec_json(node, "axi federation peers --json")
    if isinstance(peers, dict):
        peers = peers.get("peers", [])
    return {p.get("display_name") for p in peers if p.get("display_name")}


def test_hierarchical_3_level_federation(request):
    ok, reason = docker_available()
    if not ok:
        pytest.skip(f"federation_lifecycle: {reason}")

    project = f"axifed_{request.node.name}".replace("[", "_").replace("]", "_").lower()
    all_nodes = ("root", "intermediate1", "leaf1a", "leaf1b")
    leaves = ("leaf1a", "leaf1b")

    with FederationHarness(
        project=project,
        compose_file=COMPOSE,
        nodes=all_nodes,
    ) as fed:
        fed.start()

        # 1. Init identity on every node.
        for name in all_nodes:
            out = fed.exec_json(
                name,
                f"axi federation init --owner test@{name}.local --name {name} --json",
            )
            assert out["initialized"] is True

        root_id = fed.exec_json("root", "axi federation status --json")["node_id"]
        intermediate_id = fed.exec_json("intermediate1", "axi federation status --json")["node_id"]

        # 2. intermediate1 registers root (intermediate acts as leaf to root).
        result = fed.add_peer(from_node="intermediate1", to_node="root")
        assert result.get("identity_bound") is True, (
            f"intermediate1→root identity binding failed: {result}"
        )
        assert result.get("node_id") == root_id

        # 3. Each leaf registers intermediate1.
        for leaf in leaves:
            result = fed.add_peer(from_node=leaf, to_node="intermediate1")
            assert result.get("identity_bound") is True, (
                f"{leaf}→intermediate1 identity binding failed: {result}"
            )
            assert result.get("node_id") == intermediate_id

        # 4. Per-tier peer assertions.
        fed.assert_federated("intermediate1", "root")
        for leaf in leaves:
            fed.assert_federated(leaf, "intermediate1")

        # 5. Exact peer sets via `axi federation peers --json`.
        root_peers = _peer_names(fed, "root")
        assert root_peers == set(), (
            f"root should have no outbound peers (it only receives adds), got {root_peers}"
        )

        intermediate_peers = _peer_names(fed, "intermediate1")
        assert intermediate_peers == {"root"}, (
            f"intermediate1 peers expected exactly {{'root'}}, got {intermediate_peers}"
        )

        for leaf in leaves:
            lpeers = _peer_names(fed, leaf)
            assert lpeers == {"intermediate1"}, (
                f"{leaf} peers expected exactly {{'intermediate1'}}, got {lpeers}"
            )
            # Hierarchy respected: leaf has NO direct peer entry for root.
            assert "root" not in lpeers, (
                f"{leaf} unexpectedly has direct peer entry for root; "
                f"hierarchy violated. peers={lpeers}"
            )

        # 6. Shutdown intermediate1 — leaves lose hub-chain reach but
        #    keep local self-sufficiency (ADR-016).
        fed._compose("stop", "intermediate1")

        for leaf in leaves:
            # Local status must still succeed.
            status = fed.exec_json(leaf, "axi federation status --json")
            assert status.get("node_id"), (
                f"{leaf} lost its own identity when intermediate1 went down: {status}"
            )
            # Peer list still contains intermediate1 as a registry entry —
            # the local record is durable even when the peer is unreachable.
            assert "intermediate1" in _peer_names(fed, leaf), (
                f"{leaf} dropped intermediate1 from its registry on outage"
            )
