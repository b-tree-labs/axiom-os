# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RAG search endpoint: ``POST /api/v1/rag/search``.

Two kinds of caller ask it the same question.

* **A federation peer** authenticates with ``X-Node-ID`` + ``X-Signature`` and
  names an ``access_tier``; the tier maps to corpora (:func:`_tier_to_corpora`).
* **A person**, typically a laptop bound to this node, presents a gate key or a
  session. The composed app's authz hook verifies it before this handler runs
  (the mount declares the path a read, so a ``*:read`` key may ask) and binds
  the credential's site. Such a caller never chooses a tier: what it may read
  is the corpora a served surface reads for its tenant
  (``chat.scope.shared_corpora_for``), filtered by the retriever's fail-closed
  access context (tier, classification, nationality).

Either way the caller's verified site holds: a credential bound to a site that
is not this node's home site reads that site's material and the shared corpus,
never another tenant's or the operator's own. ``rag-internal`` is never
searchable from here.
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


def _tenant_corpora(tenant: str | None) -> list[str]:
    """What a served surface reads for ``tenant`` (``None``: this node's own callers).

    Raises ``ValueError`` for a malformed tenant id, which the caller turns into a refusal: a broken
    id must never widen a read.
    """
    from axiom.extensions.builtins.chat.scope import shared_corpora_for

    return list(shared_corpora_for(tenant))


def _embed(text: str) -> list[float] | None:
    """Best-effort query embedding on this node; text-only retrieval when none is configured."""
    try:
        from axiom.rag.embeddings import embed_texts

        vectors = embed_texts([text])
    except Exception:  # noqa: BLE001 — the embedder is optional
        return None
    return vectors[0] if vectors else None


def _caller_search(store, data: dict, *, principal: str, tenant: str | None, embed_fn) -> list[dict]:
    """A gate-verified person's search: the tenant's corpora, the fail-closed access filter."""
    from axiom.rag.retriever import access_context_for_principal, retrieve

    query = str(data.get("query") or "")
    embedding = data.get("query_embedding") or (embed_fn(query) if query.strip() else None)
    chunks = retrieve(
        store,
        query,
        embedding,
        corpora=_tenant_corpora(tenant),
        limit=min(int(data.get("limit", 10) or 10), 50),
        access_context=access_context_for_principal(principal),
    )
    return [
        {
            "source_path": c.source_path,
            "source_title": c.source_title,
            "chunk_text": c.chunk_text,
            "chunk_index": c.chunk_index,
            "similarity": c.similarity,
            "combined_score": c.rrf_score,
            "corpus": c.corpus,
            "access_tier": c.access_tier,
            "classification": c.classification,
        }
        for c in chunks
    ]


def handle_federation_search(
    request,
    store,
    *,
    principal: str | None = None,
    tenant: str | None = None,
    embed_fn=None,
) -> tuple[int, str]:
    """Handle a RAG search request from a federation peer or a gate-verified caller.

    Args:
        request: Object with .headers dict and .body str
        store: RAGStore instance
        principal: the handle the composed app's authz hook verified, or ``None`` when no gate ran
            (the standalone server). Read from the request state, never from the body.
        tenant: the site the verified credential is held to (``site_authority.tenant_scope()``),
            or ``None`` for this node's own callers.
        embed_fn: query embedder for a caller's search (tests inject one).

    Returns:
        (status_code, response_body_json)
    """
    # Auth check (case-insensitive — urllib lowercases header names)
    node_id = _header(request.headers, "X-Node-ID", "")
    signature = _header(request.headers, "X-Signature", "")
    if not node_id and not principal:
        return 401, json.dumps({"error": "Missing X-Node-ID header"})

    # Body size check
    body = request.body if isinstance(request.body, str) else request.body.decode("utf-8")
    if len(body) > _MAX_BODY_SIZE:
        return 413, json.dumps({"error": "Request body too large"})

    # A peer's signature, when it sent one. A caller the gate already verified does not need one,
    # and an unverifiable one does not make it a peer.
    federated = bool(node_id) and _verify_request(node_id, signature, body)
    if not federated and not principal:
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

    try:
        allowed = _tenant_corpora(tenant)
    except ValueError:
        return 403, json.dumps({"error": "this credential's site is not a usable tenant id"})

    start = time.time()
    if not federated:
        rows = _caller_search(
            store, data, principal=str(principal), tenant=tenant, embed_fn=embed_fn or _embed
        )
        log.info(
            "RAG search from %s (site %s): results=%d elapsed=%dms",
            principal, tenant or "home", len(rows), int((time.time() - start) * 1000),
        )
        return 200, json.dumps({
            "results": rows,
            "node_id": _get_local_node_id(),
            "elapsed_ms": int((time.time() - start) * 1000),
            "rag_version": "0.8",
            "generation": None,
        })

    # A peer's search — use requested generation or active generation. A peer's credential
    # bound to a tenant site still reads only what that tenant may.
    corpora = _tier_to_corpora(access_tier)
    if tenant is not None:
        corpora = [c for c in corpora if c in allowed]
        if not corpora:  # an empty filter must never reach a store that reads it as "all"
            return 200, json.dumps({"results": [], "node_id": _get_local_node_id(),
                                    "elapsed_ms": 0, "rag_version": "0.8", "generation": None})

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
