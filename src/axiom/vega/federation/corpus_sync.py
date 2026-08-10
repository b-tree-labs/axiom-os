# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Corpus sync between federation peers.

Discovers what corpora each peer has, detects newer generations,
and coordinates pack downloads for upgrades.

Usage::

    client = CorpusSyncClient()
    status = client.get_peer_status("<peer>", "http://<peer>:9877")
    upgrades = client.find_available_upgrades(local_status, status["corpora"])
"""

from __future__ import annotations

import json
import logging
import urllib.request

log = logging.getLogger(__name__)


def _fetch_peer_status(url: str, timeout: float = 5.0) -> dict:
    """Fetch corpus status from a peer's generation endpoint."""
    endpoint = f"{url.rstrip('/')}/api/v1/generations"
    req = urllib.request.Request(
        endpoint,
        headers={
            "X-Node-ID": "local",
            "X-Signature": "dev",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


class CorpusSyncClient:
    """Discovers and syncs corpus state across federation peers."""

    def get_peer_status(self, node_id: str, url: str) -> dict:
        """Get corpus status from a peer node.

        Returns dict with node_id and corpora info.
        """
        try:
            data = _fetch_peer_status(url)
            return {
                "node_id": data.get("node_id", node_id),
                "corpora": {
                    corpus: {
                        "generation": info.get("active", 1),
                        "candidate": info.get("candidate"),
                    }
                    for corpus, info in data.get("generations", {}).items()
                },
            }
        except Exception as e:
            log.warning("Could not fetch status from %s: %s", node_id, e)
            return {"node_id": node_id, "corpora": {}, "error": str(e)}

    def find_available_upgrades(
        self,
        local: dict[str, dict],
        remote: dict[str, dict],
    ) -> list[dict]:
        """Compare local and remote corpus state, find available upgrades.

        Args:
            local: {corpus_name: {"generation": N}}
            remote: {corpus_name: {"generation": N, ...}}

        Returns:
            List of upgrade opportunities
        """
        upgrades = []
        for corpus, remote_info in remote.items():
            remote_gen = remote_info.get("generation", 1)
            local_gen = local.get(corpus, {}).get("generation", 1)
            if remote_gen > local_gen:
                upgrades.append(
                    {
                        "corpus": corpus,
                        "local_generation": local_gen,
                        "remote_generation": remote_gen,
                        "remote_chunks": remote_info.get("chunks", 0),
                    }
                )
        return upgrades

    def sync_status(self, peers: list[dict]) -> dict:
        """Check all peers and return sync overview.

        Args:
            peers: List of {"node_id": str, "url": str}

        Returns:
            {"peers": [...], "available_upgrades": [...]}
        """
        peer_statuses = []

        for peer in peers:
            status = self.get_peer_status(peer["node_id"], peer["url"])
            peer_statuses.append(status)

        return {
            "peers": peer_statuses,
        }
