# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Hub-and-spoke federation with peer isolation.

Scenario (5 nodes):
  1. Start hub + leaf1 + leaf2 + leaf3 + leaf4.
  2. Each leaf `axi federation init` and `axi nodes add hub axiom@hub`.
     Leaves do NOT register each other.
  3. Verify all leaves have hub as verified peer.
  4. Verify peer isolation: no leaf can see another leaf in its
     `axi nodes list`.
  5. Remove leaf4 from its own registry (self-removal); confirm other
     leaves' hub bindings are unaffected.

Validates that `axi nodes add` creates only the edge requested and never
leaks peer topology from the hub to its leaves.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.federation_lifecycle.harness import FederationHarness, docker_available

pytestmark = pytest.mark.federation_lifecycle

COMPOSE = Path(__file__).resolve().parent / "docker-compose.hub-spoke-5.yml"


def test_hub_spoke_5_node_peer_isolation(request):
    ok, reason = docker_available()
    if not ok:
        pytest.skip(f"federation_lifecycle: {reason}")

    project = f"axifed_{request.node.name}".replace("[", "_").replace("]", "_").lower()
    leaves = ("leaf1", "leaf2", "leaf3", "leaf4")
    all_nodes = ("hub", *leaves)

    with FederationHarness(
        project=project,
        compose_file=COMPOSE,
        nodes=all_nodes,
    ) as fed:
        fed.start()

        # Init identities.
        for name in all_nodes:
            out = fed.exec_json(
                name, f"axi federation init --owner test@{name}.local --name {name} --json"
            )
            assert out["initialized"] is True

        hub_id = fed.exec_json("hub", "axi federation status --json")["node_id"]

        # Each leaf adds hub.
        for leaf in leaves:
            result = fed.add_peer(from_node=leaf, to_node="hub")
            assert result.get("identity_bound") is True, (
                f"{leaf}→hub identity binding failed: {result}"
            )
            assert result.get("node_id") == hub_id

        # All leaves have hub verified.
        for leaf in leaves:
            fed.assert_federated(leaf, "hub")

        # Peer isolation: each leaf's registry must NOT contain any other leaf.
        for leaf in leaves:
            nodes_list = fed.exec_json(leaf, "axi nodes list --json")
            # nodes list returns a bare list of node dicts
            names = {n.get("display_name") for n in nodes_list}
            for other in leaves:
                if other == leaf:
                    continue
                assert other not in names, (
                    f"{leaf} unexpectedly sees {other} in its nodes list; "
                    f"peer topology leaked. names={names}"
                )

        # Remove leaf4 from its own registry; confirm leaf1/2/3 still have hub.
        fed.exec("leaf4", "axi nodes remove hub --confirm")
        for leaf in ("leaf1", "leaf2", "leaf3"):
            fed.assert_federated(leaf, "hub")
