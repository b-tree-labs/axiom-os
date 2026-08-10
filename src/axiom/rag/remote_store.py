# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Remote retrieval store — a peer's corpus as a local ``_StoreLike``.

Makes "retrieve from a remote corpus" a first-class store backend: the rest of
the retrieval stack (``rag.retriever.retrieve``, the ``axiom_rag__retrieve`` MCP
primitive) is transport-blind, so a node with no local corpus can retrieve
entirely from a peer. The laptop->peer case is a degenerate federation.

Wire protocol is the federation contract (``POST /api/v1/rag/search``, see
``axiom.rag.federation`` and ``extensions/builtins/http/federation_endpoint``):
authenticated with ``X-Node-ID`` + ``X-Signature`` headers. The peer currently
accepts any non-empty pair in dev mode; production registers the node in the
peer's ``NodeRegistry`` and verifies an Ed25519 signature (the ``signature``
parameter is the seam for that).
"""

from __future__ import annotations

from typing import Any

import requests

from axiom.rag.store import CORPUS_COMMUNITY, SearchResult


class RemoteRetrievalError(RuntimeError):
    """A remote retrieval call failed (transport, auth, or non-200)."""


def _post_json(
    url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float
) -> tuple[int, dict[str, Any]]:
    """POST JSON and return ``(status, parsed_body)``. Module-level for test
    seams.

    Uses ``requests`` rather than ``urllib`` deliberately: ``urllib`` lowercases
    header names (``X-Node-ID`` -> ``X-node-id``), and the federation endpoint
    does a case-sensitive header lookup, so urllib requests 401 with
    "Missing X-Node-ID header". ``requests`` preserves header case.
    """
    resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
    body = resp.json() if resp.content else {}
    if not isinstance(body, dict):
        body = {"results": body} if isinstance(body, list) else {"raw": body}
    return resp.status_code, body


class RemoteRetrievalStore:
    """``_StoreLike`` backed by a peer's ``/api/v1/rag/search`` endpoint."""

    # The peer runs its own hybrid (vector + keyword) search server-side, so
    # callers should issue a SINGLE text query rather than separate vector and
    # text round-trips. Halves remote calls and the network-blip failure
    # surface. Consumed by the ``axiom_rag__retrieve`` primitive.
    does_own_hybrid = True

    def __init__(
        self,
        base_url: str,
        *,
        node_id: str = "local",
        signature: str = "dev-mode",
        access_tier: str = "community",
        timeout: float = 8.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._node_id = node_id
        self._signature = signature
        self._access_tier = access_tier
        self._timeout = timeout

    def connect(self) -> None:
        """No-op — present so callers that uniformly call ``store.connect()``
        (the pgvector path does) work against a remote store too."""
        return None

    def search(
        self,
        query_embedding: list[float] | None = None,
        query_text: str = "",
        corpora: list[str] | None = None,
        limit: int = 5,
        **_: Any,
    ) -> list[SearchResult]:
        url = f"{self._base}/api/v1/rag/search"
        payload: dict[str, Any] = {
            "query": query_text,
            "access_tier": self._access_tier,
            "limit": limit,
        }
        if query_embedding:
            payload["query_embedding"] = query_embedding
        headers = {
            "Content-Type": "application/json",
            "X-Node-ID": self._node_id,
            "X-Signature": self._signature,
        }

        try:
            status, body = _post_json(url, payload, headers, self._timeout)
        except RemoteRetrievalError:
            raise
        except Exception as exc:  # noqa: BLE001 — transport/parse failure
            raise RemoteRetrievalError(
                f"remote retrieval failed ({url}): {type(exc).__name__}: {exc}"
            ) from exc

        if status != 200:
            raise RemoteRetrievalError(
                f"remote retrieval HTTP {status} from {url}: {str(body)[:200]}"
            )

        out: list[SearchResult] = []
        for r in body.get("results", []) or []:
            similarity = float(r.get("similarity", 0.0) or 0.0)
            out.append(
                SearchResult(
                    source_path=r.get("source_path", ""),
                    source_title=r.get("source_title", ""),
                    chunk_text=r.get("chunk_text", ""),
                    chunk_index=int(r.get("chunk_index", 0) or 0),
                    similarity=similarity,
                    combined_score=float(r.get("combined_score", similarity) or similarity),
                    corpus=r.get("corpus", CORPUS_COMMUNITY),
                )
            )
        return out


__all__ = ["RemoteRetrievalError", "RemoteRetrievalStore"]
