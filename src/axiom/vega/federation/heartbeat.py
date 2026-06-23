# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Federation heartbeat — periodic liveness checks between nodes.

Checks peer health at configurable intervals. Stale nodes detected
within 2× heartbeat interval. Reports status to Tidy for alerting.

Usage::

    daemon = HeartbeatDaemon(peers=[...], interval=60)
    daemon.start()  # Background thread
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime

log = logging.getLogger(__name__)

_DEFAULT_INTERVAL = 60  # seconds
_STALE_MULTIPLIER = 2


def _fetch_health(url: str, timeout: float = 5.0) -> dict:
    """Fetch health from a peer."""
    endpoint = f"{url.rstrip('/')}/health"
    req = urllib.request.Request(endpoint)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def check_peer_health(node_id: str, url: str, timeout: float = 5.0) -> dict:
    """Check a single peer's health.

    Returns:
        {"node_id": str, "healthy": bool, "latency_ms": int, "details": dict}
    """
    start = time.time()
    try:
        details = _fetch_health(url, timeout=timeout)
        latency_ms = int((time.time() - start) * 1000)
        return {
            "node_id": node_id,
            "healthy": True,
            "latency_ms": latency_ms,
            "details": details,
            "checked_at": datetime.now(UTC).isoformat(),
        }
    except Exception as e:
        latency_ms = int((time.time() - start) * 1000)
        return {
            "node_id": node_id,
            "healthy": False,
            "latency_ms": latency_ms,
            "error": str(e),
            "checked_at": datetime.now(UTC).isoformat(),
        }


@dataclass
class PeerStatus:
    node_id: str
    url: str
    healthy: bool = True
    last_seen: str = ""
    latency_ms: int = 0
    consecutive_failures: int = 0


class HeartbeatDaemon:
    """Background daemon that monitors federation peer health."""

    def __init__(
        self,
        peers: list[dict] | None = None,
        interval: int = _DEFAULT_INTERVAL,
    ) -> None:
        self.peers = peers or []
        self.interval = interval
        self._statuses: dict[str, PeerStatus] = {}
        self._running = False
        self._thread: threading.Thread | None = None

        for peer in self.peers:
            self._statuses[peer["node_id"]] = PeerStatus(
                node_id=peer["node_id"],
                url=peer["url"],
            )

    def check_all(self) -> list[dict]:
        """Run one health check cycle against all peers."""
        results = []
        for peer in self.peers:
            result = check_peer_health(peer["node_id"], peer["url"])
            status = self._statuses.get(peer["node_id"])
            if status:
                status.healthy = result["healthy"]
                status.latency_ms = result["latency_ms"]
                if result["healthy"]:
                    status.last_seen = result["checked_at"]
                    status.consecutive_failures = 0
                else:
                    status.consecutive_failures += 1
                    if status.consecutive_failures >= _STALE_MULTIPLIER:
                        log.warning(
                            "Peer %s STALE — %d consecutive failures",
                            peer["node_id"],
                            status.consecutive_failures,
                        )
            results.append(result)
        return results

    def start(self) -> None:
        """Start background heartbeat thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        log.info(
            "Heartbeat daemon started (interval=%ds, peers=%d)", self.interval, len(self.peers)
        )

    def stop(self) -> None:
        """Stop the heartbeat thread."""
        self._running = False

    def _run(self) -> None:
        while self._running:
            try:
                self.check_all()
            except Exception as e:
                log.error("Heartbeat check failed: %s", e)
            time.sleep(self.interval)

    def get_status(self) -> dict:
        """Get current status of all peers."""
        return {
            "peers": [
                {
                    "node_id": s.node_id,
                    "healthy": s.healthy,
                    "last_seen": s.last_seen,
                    "latency_ms": s.latency_ms,
                    "consecutive_failures": s.consecutive_failures,
                }
                for s in self._statuses.values()
            ],
            "interval": self.interval,
        }
