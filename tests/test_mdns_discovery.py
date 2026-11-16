# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for mDNS federation discovery."""

from __future__ import annotations


class TestMDNSService:
    def test_importable(self):
        from axiom.vega.federation.mdns import MDNSService
        assert MDNSService is not None

    def test_service_type(self):
        from axiom.vega.federation.mdns import SERVICE_TYPE
        assert SERVICE_TYPE == "_axiom._tcp.local."

    def test_build_txt_records(self):
        from axiom.vega.federation.mdns import MDNSService

        svc = MDNSService(
            node_id="ax-7f3a2b9e",
            port=8766,
            profile="standard",
            version="0.8.1",
        )
        txt = svc._build_txt()
        assert txt["node_id"] == "ax-7f3a2b9e"
        assert txt["profile"] == "standard"
        assert txt["version"] == "0.8.1"

    def test_discovered_peers_list(self):
        from axiom.vega.federation.mdns import MDNSService

        svc = MDNSService(node_id="local", port=8766)
        # Initially empty
        assert svc.get_discovered_peers() == []


class TestMDNSListener:
    def test_importable(self):
        from axiom.vega.federation.mdns import AxiomServiceListener
        assert AxiomServiceListener is not None

    def test_listener_tracks_additions(self):
        from axiom.vega.federation.mdns import AxiomServiceListener

        listener = AxiomServiceListener()
        # Simulate a service addition
        listener._peers["example-host"] = {
            "node_id": "example-host",
            "url": "http://192.168.1.100:8766",
            "profile": "standard",
        }
        peers = listener.get_peers()
        assert len(peers) == 1
        assert peers[0]["node_id"] == "example-host"
