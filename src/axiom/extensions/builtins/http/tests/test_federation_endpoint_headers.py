# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Federation endpoint must authenticate regardless of header-name case.

HTTP header names are case-insensitive, and urllib lowercases them
(``X-Node-ID`` -> ``x-node-id``). A case-sensitive lookup silently 401s every
urllib client — including Axiom's own ``rag.federation._query_peer`` — so
cross-node federated search never worked. Regression guard.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.http.federation_endpoint import handle_federation_search


@pytest.fixture
def _dev_accept(monkeypatch):
    """Header-case tests need an unregistered node ACCEPTED so they can assert
    the parse path returns 200. Acceptance of unregistered nodes is fail-closed
    by default (AXIOM_FED_DEV_ACCEPT off); enable it here. The security default
    is covered by test_still_401_* and test_unregistered_node_rejected_by_default."""
    monkeypatch.setenv("AXIOM_FED_DEV_ACCEPT", "1")


class _Req:
    def __init__(self, headers, body):
        self.headers = headers
        self.body = body


class _Store:
    def search(self, **kwargs):
        return []


def test_accepts_lowercased_federation_headers(_dev_accept):
    req = _Req(
        {"x-node-id": "peer-1", "x-signature": "dev-mode", "content-type": "application/json"},
        '{"query": "q", "limit": 1}',
    )
    status, _ = handle_federation_search(req, _Store())
    assert status == 200


def test_accepts_canonical_case_headers(_dev_accept):
    req = _Req(
        {"X-Node-ID": "peer-1", "X-Signature": "dev-mode"},
        '{"query": "q", "limit": 1}',
    )
    status, _ = handle_federation_search(req, _Store())
    assert status == 200


def test_unregistered_node_rejected_by_default(monkeypatch):
    """Fail-closed: without AXIOM_FED_DEV_ACCEPT, an unregistered node with
    valid-shaped headers is REJECTED (no anonymous reach into CORPUS_ORG)."""
    monkeypatch.delenv("AXIOM_FED_DEV_ACCEPT", raising=False)
    req = _Req(
        {"x-node-id": "peer-1", "x-signature": "whatever"},
        '{"query": "q", "limit": 1}',
    )
    status, _ = handle_federation_search(req, _Store())
    assert status == 401


def test_still_401_when_node_id_truly_absent():
    req = _Req({"content-type": "application/json"}, '{"query": "q"}')
    status, _ = handle_federation_search(req, _Store())
    assert status == 401
