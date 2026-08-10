# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Peer version preflight: leaf refuses to bind an old peer.

Scenario:
  1. Start current-version leaf + hub-old (axi 0.10.3 stub).
  2. Leaf `axi federation init`.
  3. Leaf `axi nodes add hub-old axiom@hub-old` → must fail gracefully
     with a guided message: mentions "identity binding requires ≥ 0.10.4"
     and tells the operator to run `axi update` on the peer.
  4. No verified peer entry is persisted for the failed add.

Validates the preflight that shipped in v0.10.7 — see
``axiom.vega.federation.discovery.NodeRegistry.check_peer_version``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.federation_lifecycle.harness import FederationHarness, docker_available

pytestmark = pytest.mark.federation_lifecycle

COMPOSE = Path(__file__).resolve().parent / "docker-compose.version-preflight.yml"


def test_peer_version_preflight_refuses_old_peer(request):
    ok, reason = docker_available()
    if not ok:
        pytest.skip(f"federation_lifecycle: {reason}")

    project = f"axifed_{request.node.name}".replace("[", "_").replace("]", "_").lower()

    with FederationHarness(
        project=project,
        compose_file=COMPOSE,
        nodes=("leaf", "hub-old"),
    ) as fed:
        fed.start()

        # Init the current-version leaf; hub-old has only the axi stub,
        # no real federation init is possible (nor required — we're
        # testing the preflight refusal, which short-circuits before
        # identity fetch).
        fed.exec_json(
            "leaf",
            "axi federation init --owner test@leaf.local --name leaf --json",
        )

        # Attempt the add — must fail with guided message.
        result = fed.add_peer(from_node="leaf", to_node="hub-old")
        assert result.get("identity_bound") is False, (
            f"expected preflight refusal; got bound result: {result}"
        )
        msg = result.get("message", "") or ""
        # Accept either the spelled-out "≥ 0.10.4" or the literal token.
        assert "identity binding requires" in msg and "0.10.4" in msg, (
            f"expected preflight guidance mentioning 0.10.4; got: {msg!r}"
        )

        # No VERIFIED peer entry should exist for hub-old.
        nodes_list = fed.exec_json("leaf", "axi nodes list --json")
        verified_matches = [
            n
            for n in nodes_list
            if n.get("display_name") == "hub-old" and n.get("state") == "verified"
        ]
        assert not verified_matches, (
            f"leaf unexpectedly persisted a verified entry for hub-old: {verified_matches}"
        )
