# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Happy-path 3-node federation lifecycle.

Scenario:
  1. Start hub + leaf1 + leaf2 in isolated compose network.
  2. `axi federation init` on each node (distinct Ed25519 keypairs).
  3. From each leaf, `axi nodes add hub axiom@hub` — SSH fetch identity,
     bind pubkey, state=VERIFIED.
  4. Cross-check: each leaf's stored fingerprint for the hub equals the
     fingerprint the hub reports for itself.
  5. Teardown.

This is the simplest identity-binding exercise; everything else in the
scenario matrix (key rotation, Sybil containment, hierarchical topology,
cross-root bridges) builds on the fact that this path is green.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.federation_lifecycle


def test_happy_path_3_node_federation(harness):
    harness.start()

    # 1. Initialize identity on each node. --owner is required (non-interactive).
    for name in ("hub", "leaf1", "leaf2"):
        out = harness.exec_json(
            name,
            f"axi federation init --owner test@{name}.local --name {name} --json",
        )
        assert out["initialized"] is True
        assert out["node_id"], f"{name} got empty node_id"

    # Node IDs must all differ — fresh keypair per node.
    ids = {
        name: harness.exec_json(name, "axi federation status --json")["node_id"]
        for name in ("hub", "leaf1", "leaf2")
    }
    assert len(set(ids.values())) == 3, f"expected 3 distinct node_ids, got {ids}"

    # 2. Each leaf adds hub as a peer via SSH → identity binding.
    for leaf in ("leaf1", "leaf2"):
        result = harness.add_peer(from_node=leaf, to_node="hub")
        assert result.get("identity_bound") is True, f"{leaf}→hub identity binding failed: {result}"
        assert result.get("node_id") == ids["hub"], (
            f"{leaf} bound wrong node_id for hub: "
            f"got {result.get('node_id')}, expected {ids['hub']}"
        )

    # 3. Assert federation shape from each leaf's perspective.
    for leaf in ("leaf1", "leaf2"):
        harness.assert_federated(leaf, "hub")
