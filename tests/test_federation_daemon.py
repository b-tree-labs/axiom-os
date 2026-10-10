# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the federation daemon."""

from __future__ import annotations


class TestFederationDaemon:
    def test_importable(self):
        from axiom.vega.federation.daemon import FederationDaemon
        assert FederationDaemon is not None

    def test_init_with_peers(self):
        from axiom.vega.federation.daemon import FederationDaemon

        peers = [{"node_id": "node-a", "url": "http://node-a:9877"}]
        daemon = FederationDaemon(peers=peers)
        assert len(daemon.peers) == 1

    def test_get_state_empty(self):
        from axiom.vega.federation.daemon import FederationDaemon

        daemon = FederationDaemon()
        state = daemon.get_state()
        assert "peers" in state
        assert "resources" in state
        assert "corpus_status" in state

    def test_get_peers_for_rag(self):
        from axiom.vega.federation.daemon import FederationDaemon

        peers = [
            {"node_id": "node-a", "url": "http://node-a:9877"},
            {"node_id": "hpc", "url": "http://hpc:9877"},
        ]
        daemon = FederationDaemon(peers=peers)
        rag_peers = daemon.get_peers_for_rag()
        assert len(rag_peers) == 2

    def test_get_best_llm_no_resources(self):
        from axiom.vega.federation.daemon import FederationDaemon

        daemon = FederationDaemon()
        best = daemon.get_best_llm()
        assert best is None
