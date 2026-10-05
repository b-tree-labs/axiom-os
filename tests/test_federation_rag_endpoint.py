# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the federation RAG search endpoint and fan-out client.

TDD: tests written first, then implementation.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

from axiom.rag.store import CORPUS_COMMUNITY, CORPUS_ORG, SearchResult

# ---------------------------------------------------------------------------
# Federation RAG search endpoint tests
# ---------------------------------------------------------------------------


class TestFederationSearchEndpoint:
    """Tests for POST /api/v1/rag/search on the web API server."""

    def test_endpoint_rejects_no_auth(self):
        """POST without X-Node-ID returns 401."""
        from axiom.extensions.builtins.http.federation_endpoint import (
            handle_federation_search,
        )

        request = MagicMock()
        request.headers = {}
        request.body = json.dumps({"query": "test", "access_tier": "community"})

        status, body = handle_federation_search(request, store=MagicMock())
        assert status == 401

    def test_endpoint_returns_results(self):
        """Valid request returns search results."""
        from axiom.extensions.builtins.http.federation_endpoint import (
            handle_federation_search,
        )

        mock_store = MagicMock()
        mock_store.search.return_value = [
            SearchResult(
                source_path="doc.pdf",
                source_title="Test Doc",
                chunk_text="Relevant content here.",
                chunk_index=0,
                similarity=0.85,
                combined_score=0.82,
                corpus=CORPUS_COMMUNITY,
            )
        ]

        request = MagicMock()
        request.headers = {"X-Node-ID": "test-node", "X-Signature": "valid"}
        request.body = json.dumps({
            "query": "test query",
            "access_tier": "community",
            "limit": 5,
        })

        # Skip signature verification for this test
        with patch(
            "axiom.extensions.builtins.http.federation_endpoint._verify_request",
            return_value=True,
        ):
            status, body = handle_federation_search(request, store=mock_store)

        assert status == 200
        data = json.loads(body)
        assert "results" in data
        assert len(data["results"]) == 1
        assert data["results"][0]["source_path"] == "doc.pdf"

    def test_endpoint_filters_by_access_tier(self):
        """Community-tier caller gets no org or internal chunks."""
        from axiom.extensions.builtins.http.federation_endpoint import (
            _tier_to_corpora,
        )

        assert _tier_to_corpora("community") == [CORPUS_COMMUNITY]
        assert set(_tier_to_corpora("restricted")) == {CORPUS_COMMUNITY, CORPUS_ORG}

    def test_endpoint_never_exposes_internal(self):
        """rag-internal is NEVER shared with federation peers."""
        from axiom.extensions.builtins.http.federation_endpoint import (
            _tier_to_corpora,
        )

        for tier in ("community", "restricted", "export_controlled"):
            corpora = _tier_to_corpora(tier)
            assert "rag-internal" not in corpora, (
                f"Tier '{tier}' exposes rag-internal — SECURITY VIOLATION"
            )

    def test_endpoint_rejects_oversized_body(self):
        """Body > 64KB returns 413."""
        from axiom.extensions.builtins.http.federation_endpoint import (
            handle_federation_search,
        )

        request = MagicMock()
        request.headers = {"X-Node-ID": "test", "X-Signature": "sig"}
        request.body = "x" * 65537

        status, _ = handle_federation_search(request, store=MagicMock())
        assert status == 413


# ---------------------------------------------------------------------------
# Fan-out client tests
# ---------------------------------------------------------------------------


class TestFederatedSearch:
    """Tests for the fan-out client (rag/federation.py)."""

    def test_no_peers_returns_local_only(self):
        """No federated nodes → local results only."""
        from axiom.rag.federation import federated_search

        mock_store = MagicMock()
        mock_store.search.return_value = [
            SearchResult("doc.pdf", "Doc", "text", 0, 0.9, 0.9, CORPUS_COMMUNITY)
        ]

        results = federated_search(
            local_store=mock_store,
            query_text="test",
            peers=[],
        )
        assert len(results) == 1
        assert results[0].origin_node_id is None  # local

    def test_merges_by_score(self):
        """Local + 1 peer → merged sorted by combined_score descending."""
        from axiom.rag.federation import federated_search

        mock_store = MagicMock()
        mock_store.search.return_value = [
            SearchResult("local.pdf", "Local", "local text", 0, 0.7, 0.7, CORPUS_COMMUNITY)
        ]

        mock_peer_results = [
            {"source_path": "remote.pdf", "source_title": "Remote",
             "chunk_text": "remote text", "chunk_index": 0,
             "similarity": 0.9, "combined_score": 0.9, "corpus": CORPUS_COMMUNITY}
        ]

        with patch("axiom.rag.federation._query_peer", return_value=mock_peer_results):
            results = federated_search(
                local_store=mock_store,
                query_text="test",
                peers=[{"node_id": "peer1", "url": "https://peer1:8080"}],
            )

        assert len(results) == 2
        # Remote should be first (higher score)
        assert results[0].origin_node_id == "peer1"
        assert results[1].origin_node_id is None

    def test_peer_timeout_returns_local(self):
        """Slow peer → returns local results within timeout."""
        from axiom.rag.federation import federated_search

        mock_store = MagicMock()
        mock_store.search.return_value = [
            SearchResult("local.pdf", "Doc", "text", 0, 0.8, 0.8, CORPUS_COMMUNITY)
        ]

        def slow_peer(*args, **kwargs):
            time.sleep(5)
            return []

        with patch("axiom.rag.federation._query_peer", side_effect=slow_peer):
            start = time.time()
            results = federated_search(
                local_store=mock_store,
                query_text="test",
                peers=[{"node_id": "slow", "url": "https://slow:8080"}],
                peer_timeout=1.0,
            )
            elapsed = time.time() - start

        assert elapsed < 2.0, f"Fan-out took {elapsed:.1f}s — should timeout at 1.0s"
        assert len(results) >= 1  # At least local results

    def test_peer_error_returns_local(self):
        """Peer 500 → local results still returned."""
        from axiom.rag.federation import federated_search

        mock_store = MagicMock()
        mock_store.search.return_value = [
            SearchResult("local.pdf", "Doc", "text", 0, 0.8, 0.8, CORPUS_COMMUNITY)
        ]

        with patch("axiom.rag.federation._query_peer", side_effect=ConnectionError("fail")):
            results = federated_search(
                local_store=mock_store,
                query_text="test",
                peers=[{"node_id": "bad", "url": "https://bad:8080"}],
            )

        assert len(results) >= 1

    def test_tags_provenance(self):
        """Remote results have origin_node_id; local have None."""
        from axiom.rag.federation import federated_search

        mock_store = MagicMock()
        mock_store.search.return_value = [
            SearchResult("local.pdf", "Doc", "text", 0, 0.5, 0.5, CORPUS_COMMUNITY)
        ]

        mock_peer_results = [
            {"source_path": "remote.pdf", "source_title": "Remote",
             "chunk_text": "remote", "chunk_index": 0,
             "similarity": 0.9, "combined_score": 0.9, "corpus": CORPUS_COMMUNITY}
        ]

        with patch("axiom.rag.federation._query_peer", return_value=mock_peer_results):
            results = federated_search(
                local_store=mock_store,
                query_text="test",
                peers=[{"node_id": "node-a", "url": "https://node-a:8080"}],
            )

        local = [r for r in results if r.origin_node_id is None]
        remote = [r for r in results if r.origin_node_id is not None]
        assert len(local) >= 1
        assert len(remote) >= 1
        assert remote[0].origin_node_id == "node-a"
