# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Federation stress and scale tests.

Tests federation behavior under adversarial and high-load conditions:
- Many peers (50+)
- Slow/unreachable peers
- Concurrent fan-out
- Mixed generations
- Resource selection under churn
- Heartbeat failure cascades
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from axiom.rag.store import CORPUS_COMMUNITY, SearchResult


class TestFanOutScale:
    """Test RAG fan-out with many peers."""

    def test_fanout_with_10_peers(self):
        from axiom.rag.federation import federated_search

        mock_store = MagicMock()
        mock_store.search.return_value = [
            SearchResult("local.pdf", "Doc", "local text", 0, 0.8, 0.8, CORPUS_COMMUNITY)
        ]

        peers = [{"node_id": f"peer-{i}", "url": f"http://peer{i}:9877"} for i in range(10)]

        with patch("axiom.rag.federation._query_peer", return_value=[]):
            t0 = time.time()
            results = federated_search(mock_store, "test query", peers=peers, peer_timeout=0.5)
            elapsed = time.time() - t0

        # Should complete within timeout + overhead, not N * timeout
        assert elapsed < 2.0, f"Fan-out took {elapsed:.1f}s — should be parallel"
        assert len(results) >= 1  # At least local results

    def test_fanout_with_mixed_fast_slow_peers(self):
        from axiom.rag.federation import federated_search

        mock_store = MagicMock()
        mock_store.search.return_value = [
            SearchResult("local.pdf", "Doc", "local", 0, 0.5, 0.5, CORPUS_COMMUNITY)
        ]

        fast_result = [{"source_path": "fast.pdf", "source_title": "Fast",
                        "chunk_text": "fast result", "chunk_index": 0,
                        "similarity": 0.9, "combined_score": 0.9, "corpus": CORPUS_COMMUNITY}]

        def mock_query(peer, *args, **kwargs):
            if "slow" in peer["node_id"]:
                time.sleep(3)  # Slow peer
                return []
            return fast_result

        peers = [
            {"node_id": "fast-1", "url": "http://fast1:9877"},
            {"node_id": "slow-1", "url": "http://slow1:9877"},
            {"node_id": "fast-2", "url": "http://fast2:9877"},
        ]

        with patch("axiom.rag.federation._query_peer", side_effect=mock_query):
            t0 = time.time()
            results = federated_search(mock_store, "test", peers=peers, peer_timeout=1.0)
            elapsed = time.time() - t0

        assert elapsed < 2.0, "Slow peer should not block fast peers"
        assert len(results) >= 1

    def test_fanout_all_peers_fail(self):
        from axiom.rag.federation import federated_search

        mock_store = MagicMock()
        mock_store.search.return_value = [
            SearchResult("local.pdf", "Doc", "fallback", 0, 0.7, 0.7, CORPUS_COMMUNITY)
        ]

        peers = [{"node_id": f"dead-{i}", "url": f"http://dead{i}:9877"} for i in range(5)]

        with patch("axiom.rag.federation._query_peer", side_effect=ConnectionError("refused")):
            results = federated_search(mock_store, "test", peers=peers, peer_timeout=0.5)

        assert len(results) >= 1  # Local results survive
        assert all(r.origin_node_id is None for r in results)


class TestResourceSelectionScale:
    """Test resource selection with many resources."""

    def test_select_from_50_resources(self):
        from axiom.vega.federation.resource_selection import ResourceSelector

        resources = [
            {"node_id": f"node-{i}", "model": f"model-{i}b",
             "capability_score": i / 50.0, "latency_ms": 50 + i * 10, "available": True}
            for i in range(50)
        ]

        selector = ResourceSelector(strategy="best-of")
        best = selector.select(resources, task="synthesis")
        # Should pick the highest capability
        assert best["node_id"] == "node-49"

    def test_select_with_half_unavailable(self):
        from axiom.vega.federation.resource_selection import ResourceSelector

        resources = [
            {"node_id": f"node-{i}", "model": f"model-{i}b",
             "capability_score": i / 20.0, "latency_ms": 50, "available": i % 2 == 0}
            for i in range(20)
        ]

        selector = ResourceSelector(strategy="best-of")
        best = selector.select(resources, task="synthesis")
        assert best["available"] is True

    def test_fanout_selection_returns_only_available(self):
        from axiom.vega.federation.resource_selection import ResourceSelector

        resources = [
            {"node_id": f"n{i}", "available": i < 3}
            for i in range(10)
        ]

        selector = ResourceSelector(strategy="fan-out")
        selected = selector.select_all(resources)
        assert len(selected) == 3


class TestHeartbeatResilience:
    """Test heartbeat under failure conditions."""

    def test_consecutive_failures_mark_stale(self):
        from axiom.vega.federation.heartbeat import HeartbeatDaemon

        peers = [{"node_id": "flaky", "url": "http://flaky:9877"}]
        daemon = HeartbeatDaemon(peers=peers, interval=1)

        with patch("axiom.vega.federation.heartbeat._fetch_health",
                    side_effect=ConnectionError("timeout")):
            daemon.check_all()
            daemon.check_all()

        status = daemon.get_status()
        flaky = [p for p in status["peers"] if p["node_id"] == "flaky"][0]
        assert flaky["healthy"] is False
        assert flaky["consecutive_failures"] >= 2

    def test_recovery_resets_failures(self):
        from axiom.vega.federation.heartbeat import HeartbeatDaemon

        peers = [{"node_id": "recovering", "url": "http://rec:9877"}]
        daemon = HeartbeatDaemon(peers=peers, interval=1)

        # Fail twice
        with patch("axiom.vega.federation.heartbeat._fetch_health",
                    side_effect=ConnectionError("timeout")):
            daemon.check_all()
            daemon.check_all()

        # Recover
        with patch("axiom.vega.federation.heartbeat._fetch_health",
                    return_value={"status": "ok"}):
            daemon.check_all()

        status = daemon.get_status()
        rec = [p for p in status["peers"] if p["node_id"] == "recovering"][0]
        assert rec["healthy"] is True
        assert rec["consecutive_failures"] == 0


class TestCorpusSyncEdgeCases:
    """Test corpus sync with edge cases."""

    def test_sync_with_empty_remote(self):
        from axiom.vega.federation.corpus_sync import CorpusSyncClient

        client = CorpusSyncClient()
        local = {"rag-community": {"generation": 1}}
        remote = {}
        upgrades = client.find_available_upgrades(local, remote)
        assert len(upgrades) == 0

    def test_sync_with_new_corpus_on_remote(self):
        from axiom.vega.federation.corpus_sync import CorpusSyncClient

        client = CorpusSyncClient()
        local = {}  # No corpora locally
        remote = {"rag-community": {"generation": 5, "chunks": 50000}}
        upgrades = client.find_available_upgrades(local, remote)
        assert len(upgrades) == 1
        assert upgrades[0]["remote_generation"] == 5

    def test_sync_multiple_corpora(self):
        from axiom.vega.federation.corpus_sync import CorpusSyncClient

        client = CorpusSyncClient()
        local = {
            "rag-community": {"generation": 1},
            "rag-org": {"generation": 3},
        }
        remote = {
            "rag-community": {"generation": 2},
            "rag-org": {"generation": 2},  # Local is ahead!
        }
        upgrades = client.find_available_upgrades(local, remote)
        assert len(upgrades) == 1
        assert upgrades[0]["corpus"] == "rag-community"  # Only community needs upgrade


class TestMDNSIntegration:
    """Test mDNS wiring into federation daemon."""

    def test_daemon_accepts_mdns_flag(self):
        from axiom.vega.federation.daemon import FederationDaemon

        daemon = FederationDaemon(mdns=False)
        assert daemon._mdns_enabled is False

    def test_daemon_mdns_default_enabled(self):
        from axiom.vega.federation.daemon import FederationDaemon

        daemon = FederationDaemon()
        assert daemon._mdns_enabled is True
