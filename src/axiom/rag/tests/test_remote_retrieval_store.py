# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the remote retrieval store (WS-H).

A remote peer's corpus is exposed as an ordinary ``_StoreLike`` store so the
rest of the retrieval stack (``retrieve()``, ``axiom_rag__retrieve``) is
transport-blind. The laptop->peer case is a degenerate federation: a node whose
corpus lives entirely on a peer, reached over ``/api/v1/rag/search``.
"""

from __future__ import annotations

import pytest

from axiom.rag import remote_store as rs
from axiom.rag.remote_store import RemoteRetrievalError, RemoteRetrievalStore
from axiom.rag.store import SearchResult
from axiom.rag.store_factory import create_store


def _canned_response():
    return 200, {
        "results": [
            {
                "source_path": "corpus/triga-fuel.md",
                "source_title": "TRIGA Fuel Handling",
                "chunk_text": "Fuel elements are moved with the long-handled tool.",
                "chunk_index": 3,
                "similarity": 0.82,
                "combined_score": 0.91,
                "corpus": "rag-community",
            }
        ],
        "node_id": "peer-1",
        "elapsed_ms": 12,
    }


def test_create_store_dispatches_http_to_remote():
    assert isinstance(create_store("http://10.0.0.1:8766"), RemoteRetrievalStore)
    assert isinstance(create_store("https://example.org"), RemoteRetrievalStore)


def test_search_maps_results_to_searchresult(monkeypatch):
    monkeypatch.setattr(rs, "_post_json", lambda url, payload, headers, timeout: _canned_response())
    store = RemoteRetrievalStore("http://host:8766")
    out = store.search(query_text="fuel handling", limit=5)
    assert len(out) == 1
    r = out[0]
    assert isinstance(r, SearchResult)
    assert r.source_path == "corpus/triga-fuel.md"
    assert r.chunk_index == 3
    assert r.combined_score == 0.91
    assert r.corpus == "rag-community"


def test_search_sends_federation_auth_headers(monkeypatch):
    captured = {}

    def _spy(url, payload, headers, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["payload"] = payload
        return _canned_response()

    monkeypatch.setattr(rs, "_post_json", _spy)
    RemoteRetrievalStore("http://host:8766", node_id="laptop-1").search(query_text="q", limit=3)
    assert captured["url"] == "http://host:8766/api/v1/rag/search"
    assert captured["headers"]["X-Node-ID"] == "laptop-1"
    assert captured["headers"]["X-Signature"]  # non-empty (dev-mode placeholder ok)
    assert captured["payload"]["query"] == "q"
    assert captured["payload"]["limit"] == 3


def test_non_200_raises(monkeypatch):
    monkeypatch.setattr(rs, "_post_json", lambda *a, **k: (401, {"error": "Missing X-Node-ID header"}))
    with pytest.raises(RemoteRetrievalError):
        RemoteRetrievalStore("http://host:8766").search(query_text="q")


def test_transport_error_raises_remote_error(monkeypatch):
    def _boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(rs, "_post_json", _boom)
    with pytest.raises(RemoteRetrievalError):
        RemoteRetrievalStore("http://host:8766").search(query_text="q")


def test_end_to_end_via_rag_retrieve(monkeypatch):
    """axiom_rag__retrieve over a RemoteRetrievalStore — the laptop proof path."""
    import asyncio

    from axiom.extensions.builtins.mcp import platform_primitives as pp

    monkeypatch.setattr(rs, "_post_json", lambda *a, **k: _canned_response())
    monkeypatch.setattr(pp, "_resolve_rag_store", lambda: RemoteRetrievalStore("http://host:8766"))
    monkeypatch.setattr(pp, "_embed_query", lambda q: None)  # text-only -> single remote call
    out = asyncio.run(pp._rag_retrieve({"query": "fuel handling", "k": 5}))
    assert out["ok"] is True
    assert out["count"] == 1
    assert out["results"][0]["source_path"] == "corpus/triga-fuel.md"
