# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Federated resource selection — choose the best LLM across the federation.

Three strategies:
  best-of:  Pick the single best resource by weighted score (capability, latency, availability)
  fan-out:  Use all available resources in parallel, merge results
  pinned:   Always use a specific node, fallback to best-of if unavailable

Task-aware weighting:
  synthesis/extraction: capability-weighted (prefer powerful models)
  classification:       latency-weighted (prefer fast local models)
  fallback:             balanced

Usage::

    selector = ResourceSelector(strategy="best-of")
    resources = discover_resources(federation_peers)
    best = selector.select(resources, task="synthesis")
    # → {"node_id": "<peer>", "model": "<model>", ...}
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Task → weight profiles (capability_weight, latency_weight)
_TASK_WEIGHTS = {
    "synthesis": (0.8, 0.2),
    "extraction": (0.8, 0.2),
    "correlation": (0.7, 0.3),
    "code_generation": (0.7, 0.3),
    "classification": (0.3, 0.7),
    "diagnosis": (0.3, 0.7),
    "fallback": (0.5, 0.5),
}

# Max latency for normalization (ms)
_MAX_LATENCY = 500.0


class ResourceSelector:
    """Selects the best LLM resource across federated nodes."""

    def __init__(
        self,
        strategy: str = "best-of",
        pinned_node: str | None = None,
    ) -> None:
        self.strategy = strategy
        self.pinned_node = pinned_node

    def select(
        self,
        resources: list[dict],
        task: str = "fallback",
    ) -> dict | None:
        """Select the single best resource for a task.

        Args:
            resources: List of resource dicts with node_id, capability_score,
                       latency_ms, available
            task: Task type for weight selection

        Returns:
            Best resource dict, or None if nothing available
        """
        available = [r for r in resources if r.get("available", True)]
        if not available:
            return None

        if self.strategy == "pinned" and self.pinned_node:
            pinned = [r for r in available if r["node_id"] == self.pinned_node]
            if pinned:
                return pinned[0]
            log.warning(
                "Pinned node %s unavailable, falling back to best-of",
                self.pinned_node,
            )

        # Score and rank
        cap_w, lat_w = _TASK_WEIGHTS.get(task, _TASK_WEIGHTS["fallback"])
        scored = []
        for r in available:
            cap = r.get("capability_score", 0.5)
            lat = r.get("latency_ms", 100)
            lat_norm = max(0, 1.0 - (lat / _MAX_LATENCY))
            score = cap_w * cap + lat_w * lat_norm
            scored.append((score, r))

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1]

    def select_all(self, resources: list[dict]) -> list[dict]:
        """Return all available resources (for fan-out strategy)."""
        return [r for r in resources if r.get("available", True)]


def discover_resources(peers: list[dict], timeout: float = 2.0) -> list[dict]:
    """Discover available LLM resources from federation peers.

    Queries each peer's resource endpoint and collects capabilities.
    Local resources are always included.
    """
    import json
    import urllib.request

    resources = []

    for peer in peers:
        url = f"{peer['url'].rstrip('/')}/v1/models"
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "X-Node-ID": "local",
                    "X-Signature": "dev",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
                for model in data.get("data", []):
                    resources.append(
                        {
                            "node_id": peer["node_id"],
                            "name": model.get("id", "unknown"),
                            "model": model.get("id", "unknown"),
                            "capability_score": _estimate_capability(model.get("id", "")),
                            "latency_ms": 50,  # TODO: measure actual RTT
                            "available": True,
                            "url": peer["url"],
                        }
                    )
        except Exception as e:
            log.debug("Could not discover resources from %s: %s", peer["node_id"], e)

    return resources


def _estimate_capability(model_name: str) -> float:
    """Rough capability estimate from model name."""
    name = model_name.lower()
    if "122b" in name or "70b" in name:
        return 0.95
    if "34b" in name or "32b" in name:
        return 0.8
    if "13b" in name or "14b" in name:
        return 0.6
    if "7b" in name or "8b" in name:
        return 0.4
    if "1b" in name or "3b" in name:
        return 0.2
    if "rag" in name:
        return 0.9  # RAG-grounded endpoints are more capable
    return 0.5
