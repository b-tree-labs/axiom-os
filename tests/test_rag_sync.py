# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for RAG corpus sync between federation peers (P2.9)."""

from __future__ import annotations

from unittest.mock import patch


class TestCorpusSync:
    def test_importable(self):
        from axiom.vega.federation.corpus_sync import CorpusSyncClient
        assert CorpusSyncClient is not None

    def test_discover_peer_corpus(self):
        """Should discover what corpora a peer has."""
        from axiom.vega.federation.corpus_sync import CorpusSyncClient

        mock_peer_response = {
            "node_id": "example-host",
            "generations": {
                "rag-community": {"active": 2, "candidate": None},
                "rag-org": {"active": 1, "candidate": None},
            }
        }
        with patch("axiom.vega.federation.corpus_sync._fetch_peer_status",
                    return_value=mock_peer_response):
            client = CorpusSyncClient()
            status = client.get_peer_status("example-host", "http://localhost:9877")
            assert status["corpora"]["rag-community"]["generation"] == 2

    def test_detect_newer_generation(self):
        from axiom.vega.federation.corpus_sync import CorpusSyncClient

        client = CorpusSyncClient()
        local = {"rag-community": {"generation": 1}}
        remote = {"rag-community": {"generation": 2, "chunks": 35000}}
        upgrades = client.find_available_upgrades(local, remote)
        assert len(upgrades) == 1
        assert upgrades[0]["corpus"] == "rag-community"
        assert upgrades[0]["remote_generation"] == 2

    def test_no_upgrade_when_current(self):
        from axiom.vega.federation.corpus_sync import CorpusSyncClient

        client = CorpusSyncClient()
        local = {"rag-community": {"generation": 2}}
        remote = {"rag-community": {"generation": 2, "chunks": 35000}}
        upgrades = client.find_available_upgrades(local, remote)
        assert len(upgrades) == 0

    def test_no_upgrade_when_ahead(self):
        from axiom.vega.federation.corpus_sync import CorpusSyncClient

        client = CorpusSyncClient()
        local = {"rag-community": {"generation": 3}}
        remote = {"rag-community": {"generation": 2, "chunks": 35000}}
        upgrades = client.find_available_upgrades(local, remote)
        assert len(upgrades) == 0


class TestHeartbeat:
    def test_importable(self):
        from axiom.vega.federation.heartbeat import HeartbeatDaemon
        assert HeartbeatDaemon is not None

    def test_check_peer_health(self):
        from axiom.vega.federation.heartbeat import check_peer_health

        with patch("axiom.vega.federation.heartbeat._fetch_health",
                    return_value={"status": "ok", "node": "example-host"}):
            result = check_peer_health("example-host", "http://localhost:9877")
            assert result["healthy"] is True

    def test_detect_stale_peer(self):
        from axiom.vega.federation.heartbeat import check_peer_health

        with patch("axiom.vega.federation.heartbeat._fetch_health",
                    side_effect=ConnectionError("timeout")):
            result = check_peer_health("example-host", "http://localhost:9877")
            assert result["healthy"] is False
