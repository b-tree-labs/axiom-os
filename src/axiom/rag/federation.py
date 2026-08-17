# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Federated RAG search — fan-out to peer nodes.

Queries local store + all registered peers in parallel,
merges results by score, deduplicates, and returns with provenance.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .store import CORPUS_COMMUNITY, SearchResult

log = logging.getLogger(__name__)


@dataclass
class FederatedSearchResult:
    """A search result with provenance tracking."""

    result: SearchResult
    origin_node_id: str | None = None  # None = local

    # Delegate attribute access to the inner result for convenience
    @property
    def source_path(self) -> str:
        return self.result.source_path

    @property
    def combined_score(self) -> float:
        return self.result.combined_score


def _query_peer(
    peer: dict,
    query_text: str,
    query_embedding: list[float] | None = None,
    access_tier: str = "community",
    limit: int = 10,
    timeout: float = 1.5,
) -> list[dict]:
    """Query a single peer's federation search endpoint.

    Args:
        peer: {"node_id": str, "url": str}
        query_text: The query string
        query_embedding: Optional pre-computed embedding (saves peer re-embedding)
        access_tier: Access tier for the request
        limit: Max results to request
        timeout: HTTP timeout in seconds

    Returns:
        List of result dicts from the peer, or empty list on failure.
    """
    url = f"{peer['url'].rstrip('/')}/api/v1/rag/search"

    payload = {
        "query": query_text,
        "access_tier": access_tier,
        "limit": limit,
    }
    if query_embedding:
        payload["query_embedding"] = query_embedding

    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Node-ID": "local",  # TODO: use actual local node ID
        "X-Signature": "dev-mode",  # TODO: sign with Ed25519
    }

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    start = time.time()

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
            rtt_ms = int((time.time() - start) * 1000)
            log.info(
                "Peer %s responded: %d results in %dms",
                peer["node_id"],
                len(body.get("results", [])),
                rtt_ms,
            )
            return body.get("results", [])
    except Exception as e:
        rtt_ms = int((time.time() - start) * 1000)
        log.warning("Peer %s failed after %dms: %s", peer["node_id"], rtt_ms, e)
        return []


def federated_search(
    local_store,
    query_text: str,
    query_embedding: list[float] | None = None,
    peers: list[dict] | None = None,
    corpora: list[str] | None = None,
    limit: int = 10,
    access_tier: str = "community",
    peer_timeout: float = 1.5,
) -> list[FederatedSearchResult]:
    """Search local store + all peers in parallel, merge by score.

    Args:
        local_store: RAGStore instance for local search
        query_text: The query string
        query_embedding: Pre-computed embedding (sent to peers to avoid re-embedding)
        peers: List of {"node_id": str, "url": str} dicts
        corpora: Corpora to search locally
        limit: Max total results
        access_tier: Access tier for peer requests
        peer_timeout: HTTP timeout for peer requests

    Returns:
        List of FederatedSearchResult sorted by combined_score descending.
    """
    if peers is None:
        peers = []

    results: list[FederatedSearchResult] = []

    # Local search
    local_results = local_store.search(
        query_embedding=query_embedding,
        query_text=query_text,
        corpora=corpora,
        limit=limit,
    )
    for r in local_results:
        results.append(FederatedSearchResult(result=r, origin_node_id=None))

    # Peer search in parallel (daemon threads — don't block on slow peers)
    if peers:
        pool = ThreadPoolExecutor(max_workers=min(len(peers), 8))
        futures = {
            pool.submit(
                _query_peer, peer, query_text, query_embedding, access_tier, limit, peer_timeout
            ): peer
            for peer in peers
        }

        deadline = time.time() + peer_timeout + 0.5
        for future in futures:
            remaining = deadline - time.time()
            if remaining <= 0:
                log.warning("Federation fan-out deadline exceeded — skipping remaining peers")
                break
            peer = futures[future]
            try:
                peer_results = future.result(timeout=max(remaining, 0.1))
                for r in peer_results:
                    results.append(
                        FederatedSearchResult(
                            result=SearchResult(
                                source_path=r.get("source_path", ""),
                                source_title=r.get("source_title", ""),
                                chunk_text=r.get("chunk_text", ""),
                                chunk_index=r.get("chunk_index", 0),
                                similarity=r.get("similarity", 0.0),
                                combined_score=r.get("combined_score", 0.0),
                                corpus=r.get("corpus", CORPUS_COMMUNITY),
                            ),
                            origin_node_id=peer["node_id"],
                        )
                    )
            except Exception as e:
                log.warning("Peer %s failed: %s", peer["node_id"], e)

        # Don't wait for slow threads — let them die with the pool
        pool.shutdown(wait=False)

    # Sort by score descending, deduplicate by chunk text hash
    seen = set()
    deduped = []
    for r in sorted(results, key=lambda x: x.result.combined_score, reverse=True):
        key = hash(r.result.chunk_text[:200])
        if key not in seen:
            seen.add(key)
            deduped.append(r)

    return deduped[:limit]
