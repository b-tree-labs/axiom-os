# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Federation RAG search endpoint.

Handles POST /api/v1/rag/search from federation peers.
Authenticates via X-Node-ID + X-Signature headers.
Never exposes rag-internal corpus to peers.
"""

from __future__ import annotations

import json
import logging
import os
import time

from axiom.rag.store import CORPUS_COMMUNITY, CORPUS_ORG

log = logging.getLogger(__name__)

_MAX_BODY_SIZE = 65536  # 64KB


def _header(headers, name: str, default: str = "") -> str:
    """Case-insensitive header lookup.

    HTTP header names are case-insensitive, and urllib lowercases them on the
    wire (``X-Node-ID`` -> ``x-node-id``). A case-sensitive lookup silently
    401s every urllib client — including ``rag.federation._query_peer`` — so
    federation never worked cross-node. Always resolve case-insensitively.
    """
    lowered = name.lower()
    try:
        items = list(headers.items())
    except AttributeError:
        getter = getattr(headers, "get", None)
        return getter(name, default) if getter is not None else default
    for key, value in items:
        if str(key).lower() == lowered:
            return value
    return default


def _tier_to_corpora(access_tier: str) -> list[str]:
    """Map access tier to searchable corpora. Never includes rag-internal."""
    if access_tier == "restricted":
        return [CORPUS_COMMUNITY, CORPUS_ORG]
    # Default: community only (safest)
    return [CORPUS_COMMUNITY]


def _verify_request(node_id: str, signature: str, body: str) -> bool:
    """Verify Ed25519 signature from a known peer.

    TODO: Full implementation requires NodeRegistry lookup + Ed25519 verify.
    For now, accepts any non-empty node_id+signature (development mode).
    """
    try:
        from axiom.vega.federation.discovery import NodeRegistry

        registry = NodeRegistry()
        node = registry.get(node_id)
        if node is not None:
            # TODO: Verify Ed25519 signature using node.public_key
            return True
        # Node not in registry — fall through to dev-mode check
    except Exception:
        pass

    # Development/bootstrap mode: accept an unregistered node with valid-shaped
    # headers. This is FAIL-OPEN (any node_id+signature reaches CORPUS_ORG via
    # _tier_to_corpora), so it is OFF by default and must be explicitly enabled
    # with AXIOM_FED_DEV_ACCEPT=1. Without that flag an unregistered node is
    # rejected — the safe default now that this route is exposed on the composed
    # serve app. Real Ed25519 verification against the registry is still TODO.
    dev_accept = os.environ.get("AXIOM_FED_DEV_ACCEPT") in {"1", "true", "yes"}
    if dev_accept and node_id and signature:
        log.warning(
            "Accepting federation request from unregistered node %s "
            "(AXIOM_FED_DEV_ACCEPT dev mode — NOT for production)", node_id)
        return True
    return False


def handle_federation_search(request, store) -> tuple[int, str]:
    """Handle a federation RAG search request.

    Args:
        request: Object with .headers dict and .body str
        store: RAGStore instance

    Returns:
        (status_code, response_body_json)
    """
    # Auth check (case-insensitive — urllib lowercases header names)
    node_id = _header(request.headers, "X-Node-ID", "")
    signature = _header(request.headers, "X-Signature", "")
    if not node_id:
        return 401, json.dumps({"error": "Missing X-Node-ID header"})

    # Body size check
    body = request.body if isinstance(request.body, str) else request.body.decode("utf-8")
    if len(body) > _MAX_BODY_SIZE:
        return 413, json.dumps({"error": "Request body too large"})

    # Verify signature
    if not _verify_request(node_id, signature, body):
        return 401, json.dumps({"error": "Invalid signature or unknown node"})

    # Parse request
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return 400, json.dumps({"error": "Invalid JSON"})

    query = data.get("query", "")
    access_tier = data.get("access_tier", "community")
    limit = min(data.get("limit", 10), 50)  # Cap at 50
    query_embedding = data.get("query_embedding")
    requested_generation = data.get("generation")  # Optional: specific generation

    if not query and not query_embedding:
        return 400, json.dumps({"error": "query or query_embedding required"})

    # Search — use requested generation or active generation
    corpora = _tier_to_corpora(access_tier)
    start = time.time()

    search_kwargs: dict = {
        "query_embedding": query_embedding,
        "query_text": query,
        "corpora": corpora,
        "limit": limit,
    }
    if requested_generation is not None:
        search_kwargs["corpus_generation"] = int(requested_generation)

    results = store.search(**search_kwargs)

    elapsed_ms = int((time.time() - start) * 1000)

    # Format response
    response = {
        "results": [
            {
                "source_path": r.source_path,
                "source_title": r.source_title,
                "chunk_text": r.chunk_text,
                "chunk_index": r.chunk_index,
                "similarity": r.similarity,
                "combined_score": r.combined_score,
                "corpus": r.corpus,
            }
            for r in results
        ],
        "node_id": _get_local_node_id(),
        "elapsed_ms": elapsed_ms,
        "rag_version": "0.8",
        "generation": requested_generation,
    }

    log.info(
        "Federation search from %s: query=%s tier=%s results=%d elapsed=%dms",
        node_id,
        query[:50],
        access_tier,
        len(results),
        elapsed_ms,
    )

    return 200, json.dumps(response)


def _get_local_node_id() -> str:
    """Get the local node's ID."""
    try:
        from axiom.vega.federation.identity import load_identity

        identity = load_identity()
        return identity.node_id if identity else "unknown"
    except Exception:
        return "unknown"
