# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Federation daemon — discovers peers, monitors health, syncs resources.

Runs as a background thread that:
1. Discovers peers (from registry or config)
2. Heartbeats peers periodically
3. Discovers LLM resources and feeds into Gateway
4. Discovers RAG corpora and feeds into search fan-out
5. Detects available corpus upgrades

This replaces the manual SSH tunnels + static config approach
with automatic federation management.

Usage::

    from axiom.vega.federation.daemon import FederationDaemon

    daemon = FederationDaemon(
        peers=[{"node_id": "<peer>", "url": "http://<peer>:9877"}],
        gateway=gateway,  # LLM Gateway to update with discovered resources
    )
    daemon.start()
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from .corpus_sync import CorpusSyncClient
from .heartbeat import HeartbeatDaemon
from .resource_selection import ResourceSelector, discover_resources

log = logging.getLogger(__name__)

_DEFAULT_SYNC_INTERVAL = 300  # 5 minutes


@dataclass
class FederationState:
    """Current state of the federation from this node's perspective."""

    peers: dict = field(default_factory=dict)  # node_id → health info
    resources: list = field(default_factory=list)  # discovered LLM resources
    corpus_status: dict = field(default_factory=dict)  # peer corpus generations
    available_upgrades: list = field(default_factory=list)


class FederationDaemon:
    """Background daemon that manages federation discovery, health, and sync."""

    def __init__(
        self,
        peers: list[dict] | None = None,
        gateway=None,
        sync_interval: int = _DEFAULT_SYNC_INTERVAL,
        heartbeat_interval: int = 60,
        mdns: bool = True,
    ) -> None:
        self.peers = peers or []
        self.gateway = gateway
        self.sync_interval = sync_interval
        self._state = FederationState()
        self._heartbeat = HeartbeatDaemon(
            peers=self.peers,
            interval=heartbeat_interval,
        )
        self._sync_client = CorpusSyncClient()
        self._selector = ResourceSelector(strategy="best-of")
        self._mdns = None
        self._mdns_enabled = mdns
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the federation daemon (heartbeat + sync + mDNS)."""
        if self._running:
            return
        self._running = True
        self._heartbeat.start()

        # Start mDNS discovery if enabled
        if self._mdns_enabled:
            try:
                from .mdns import MDNSService

                self._mdns = MDNSService(port=8766)
                self._mdns.start()
                log.info("mDNS discovery enabled")
            except Exception as e:
                log.warning("mDNS startup failed: %s", e)
        self._thread = threading.Thread(target=self._sync_loop, daemon=True)
        self._thread.start()
        log.info(
            "Federation daemon started: %d peers, sync=%ds, heartbeat=%ds",
            len(self.peers),
            self.sync_interval,
            self._heartbeat.interval,
        )

    def stop(self) -> None:
        """Stop the daemon."""
        self._running = False
        self._heartbeat.stop()
        if self._mdns:
            self._mdns.stop()

    def _sync_loop(self) -> None:
        """Periodic sync: discover resources + check for corpus upgrades."""
        # Initial sync immediately
        self._run_sync()

        while self._running:
            time.sleep(self.sync_interval)
            if self._running:
                self._run_sync()

    def _run_sync(self) -> None:
        """One sync cycle."""
        try:
            # Merge static peers with mDNS-discovered peers
            all_peers = list(self.peers)
            if self._mdns:
                mdns_peers = self._mdns.get_discovered_peers()
                known_ids = {p["node_id"] for p in all_peers}
                for mp in mdns_peers:
                    if mp["node_id"] not in known_ids:
                        all_peers.append(mp)
                        log.info("mDNS peer added to sync: %s", mp["node_id"])

            # Discover LLM resources from healthy peers
            healthy_peers = [
                p
                for p in all_peers
                if self._heartbeat._statuses.get(p["node_id"], None) is None
                or self._heartbeat._statuses[p["node_id"]].healthy
            ]

            resources = discover_resources(healthy_peers)
            self._state.resources = resources

            if resources and self.gateway:
                self._update_gateway(resources)

            # Check corpus status
            for peer in healthy_peers:
                status = self._sync_client.get_peer_status(peer["node_id"], peer["url"])
                self._state.corpus_status[peer["node_id"]] = status

            log.debug(
                "Federation sync: %d resources, %d peer corpora",
                len(resources),
                len(self._state.corpus_status),
            )

        except Exception as e:
            log.warning("Federation sync failed: %s", e)

    def _update_gateway(self, resources: list[dict]) -> None:
        """Feed discovered LLM resources into the Gateway."""
        if not self.gateway:
            return

        # This would add discovered providers to the gateway
        # For now, just log what we found
        for r in resources:
            log.info(
                "Discovered LLM: %s on %s (capability=%.2f)",
                r.get("model", "?"),
                r.get("node_id", "?"),
                r.get("capability_score", 0),
            )

    def get_state(self) -> dict:
        """Get current federation state."""
        return {
            "peers": self._heartbeat.get_status(),
            "resources": self._state.resources,
            "corpus_status": self._state.corpus_status,
        }

    def get_best_llm(self, task: str = "synthesis") -> dict | None:
        """Get the best available LLM for a task."""
        return self._selector.select(self._state.resources, task=task)

    def get_peers_for_rag(self) -> list[dict]:
        """Get healthy peers for RAG fan-out."""
        return [
            {"node_id": p["node_id"], "url": p["url"]}
            for p in self.peers
            if self._heartbeat._statuses.get(p["node_id"], None) is None
            or self._heartbeat._statuses[p["node_id"]].healthy
        ]
