# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A colleague's gate key can search the node's documents, held to its own site and tier.

The search route spoke only the federation headers, so a person holding a gate key, the
credential a bound laptop has, got 401. It now sits behind the same authz hook as every other
mount, declared a read, and what it returns is held to the caller's verified site: a key for one
site never reads another's material, the node's own operator corpus is never searchable, and a
chunk above the caller's tier never comes back, whatever the request body asks for.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from axiom.extensions.builtins.http.authz_hook import build_authz_hook, build_bearer_resolver
from axiom.extensions.builtins.http.compose import compose_app
from axiom.extensions.builtins.http.middleware import MiddlewareConfig
from axiom.extensions.builtins.http.mounts import BUILTIN_MOUNT_FACTORIES, rag_search_mount_spec
from axiom.extensions.builtins.http.registry import RouterRegistry
from axiom.governance import Decision, Verdict
from axiom.rag.store import SearchResult

HOME = "alpha-site"
GUEST = "beta-site"
PATH = "/api/v1/rag/search"


def _chunk(path, corpus, *, tier="public"):
    return SearchResult(
        source_path=path,
        source_title=path,
        chunk_text=f"archive retention text from {path}",
        chunk_index=0,
        similarity=0.0,
        combined_score=0.5,
        corpus=corpus,
        access_tier=tier,
    )


class _Store:
    """A seeded corpus that honours the corpus filter the way the real store does."""

    def __init__(self) -> None:
        self.chunks = [
            _chunk("home/operating-notes.md", "rag-org"),
            _chunk("home/restricted-notes.md", "rag-org", tier="institutional"),
            _chunk("shared/reference.md", "rag-community"),
            _chunk("node/transcript.md", "rag-internal"),
            _chunk("guest/own-notes.md", f"rag-tenant:{GUEST}"),
            _chunk("other/neighbour.md", "rag-tenant:gamma-site"),
        ]
        self.asked: list = []

    def search(self, query_embedding=None, query_text="", corpora=None, limit=5, **_):
        self.asked.append(corpora)
        return [c for c in self.chunks if corpora is None or c.corpus in corpora][:limit]


class _Keys:
    """The issued-keys store shape: token -> identity (principal, scopes, site)."""

    def __init__(self, table: dict) -> None:
        self._table = table

    def resolve(self, token):
        found = self._table.get(token)
        if found is None:
            return None
        principal, scopes, site = found
        return SimpleNamespace(
            principal=principal, scopes=scopes, key_id=token[-4:], name="k", site=site
        )


KEYS = {
    "axk_home_reader": ("@reader:alpha-site", ("*:read",), HOME),
    "axk_guest_reader": ("@reviewer:beta-site", ("*:read",), GUEST),
    "axk_ingest_only": ("@pusher:alpha-site", ("ingest:invoke",), HOME),
}


def _permit(_envelope):
    return Verdict.from_decision(Decision.PERMIT, "test permits", f"t-{uuid.uuid4().hex}")


@pytest.fixture
def node(monkeypatch):
    monkeypatch.setenv("AXIOM_SITE", HOME)
    monkeypatch.delenv("AXIOM_HOME_SITES", raising=False)
    monkeypatch.delenv("AXIOM_FED_DEV_ACCEPT", raising=False)
    store = _Store()
    registry = RouterRegistry()
    registry.register(rag_search_mount_spec(lambda: store, embed_fn=lambda _q: None))
    hook = build_authz_hook(
        resolve_principal=build_bearer_resolver({}, api_keys=_Keys(KEYS)), decide_fn=_permit
    )
    app = compose_app(
        registry=registry,
        include_builtins=False,
        middleware=MiddlewareConfig(authz=hook),
        auto_authz=False,
    )
    return TestClient(app), store


def _search(client, key=None, **body):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    payload = {"query": "archive retention", "limit": 10, **body}
    return client.post(PATH, json=payload, headers=headers)


def _paths(response):
    return {r["source_path"] for r in response.json().get("results", [])}


def test_a_read_key_for_the_home_site_gets_its_documents(node):
    client, _ = node
    response = _search(client, "axk_home_reader")
    assert response.status_code == 200, response.text
    paths = _paths(response)
    assert "home/operating-notes.md" in paths and "shared/reference.md" in paths
    assert "node/transcript.md" not in paths  # the operator's own corpus is never searchable
    assert "guest/own-notes.md" not in paths and "other/neighbour.md" not in paths


def test_a_chunk_above_the_callers_tier_never_comes_back(node):
    client, _ = node
    paths = _paths(_search(client, "axk_home_reader", access_tier="restricted"))
    assert "home/restricted-notes.md" not in paths


def test_no_key_gets_nothing(node):
    client, store = node
    response = _search(client)
    assert response.status_code in (401, 403)
    assert not response.json().get("results")
    assert store.asked == []  # refused before the corpus was touched


def test_a_key_for_another_site_never_reads_the_home_sites_material(node):
    client, _ = node
    response = _search(client, "axk_guest_reader", access_tier="restricted")
    assert response.status_code == 200, response.text
    paths = _paths(response)
    assert "home/operating-notes.md" not in paths
    assert "home/restricted-notes.md" not in paths
    assert "other/neighbour.md" not in paths
    assert "node/transcript.md" not in paths
    assert paths <= {"guest/own-notes.md", "shared/reference.md"}
    assert "guest/own-notes.md" in paths


def test_a_key_whose_scopes_do_not_cover_search_is_refused(node):
    client, store = node
    response = _search(client, "axk_ingest_only")
    assert response.status_code == 403
    assert store.asked == []


def test_a_guest_peer_signature_does_not_widen_a_tenant_read(node, monkeypatch):
    """A tenant key that also sends peer headers still reads only what its tenant may."""
    monkeypatch.setenv("AXIOM_FED_DEV_ACCEPT", "1")
    client, _ = node
    response = client.post(
        PATH,
        json={"query": "archive retention", "access_tier": "restricted", "limit": 10},
        headers={
            "Authorization": "Bearer axk_guest_reader",
            "X-Node-ID": "pretend-peer",
            "X-Signature": "anything",
        },
    )
    assert response.status_code == 200
    assert "home/operating-notes.md" not in _paths(response)


def test_the_search_path_is_declared_a_read():
    spec = rag_search_mount_spec(lambda: _Store())
    assert spec.reads_path(PATH)
    assert spec.requires_authz
    assert rag_search_mount_spec in BUILTIN_MOUNT_FACTORIES


def test_a_federation_peer_with_its_signature_still_searches(monkeypatch):
    """The standalone path: no gate ran, a registered or dev-accepted peer signs."""
    from axiom.extensions.builtins.http.federation_endpoint import handle_federation_search

    monkeypatch.setenv("AXIOM_FED_DEV_ACCEPT", "1")
    store = _Store()
    req = SimpleNamespace(
        headers={"X-Node-ID": "peer-1", "X-Signature": "shared-secret"},
        body='{"query": "archive", "access_tier": "restricted"}',
    )
    status, body = handle_federation_search(req, store)
    assert status == 200
    assert store.asked == [["rag-community", "rag-org"]]
    unsigned = SimpleNamespace(headers={}, body='{"query": "archive"}')
    assert handle_federation_search(unsigned, store)[0] == 401


def test_the_search_is_gated_beside_the_web_apps_api_mount(monkeypatch):
    """The web app claims ``/api/v1`` and gates itself; the search must still get the hook."""
    from fastapi import APIRouter

    from axiom.extensions.builtins.http.registry import MountSpec

    monkeypatch.setenv("AXIOM_SITE", HOME)
    store = _Store()
    registry = RouterRegistry()
    registry.register(
        MountSpec(prefix="/api/v1", router=APIRouter(), extension="webapp", requires_authz=False)
    )
    registry.register(rag_search_mount_spec(lambda: store, embed_fn=lambda _q: None))
    hook = build_authz_hook(
        resolve_principal=build_bearer_resolver({}, api_keys=_Keys(KEYS)), decide_fn=_permit
    )
    client = TestClient(
        compose_app(
            registry=registry,
            include_builtins=False,
            middleware=MiddlewareConfig(authz=hook),
            auto_authz=False,
        )
    )
    assert "home/operating-notes.md" in _paths(_search(client, "axk_home_reader"))
    guest = _paths(_search(client, "axk_guest_reader"))
    assert "home/operating-notes.md" not in guest and "guest/own-notes.md" in guest
    assert _search(client).status_code in (401, 403)


def test_nesting_needs_a_declaration():
    from fastapi import APIRouter

    from axiom.extensions.builtins.http.registry import MountSpec, PrefixConflictError

    registry = RouterRegistry()
    registry.register(MountSpec(prefix="/api/v1", router=APIRouter(), extension="outer"))
    with pytest.raises(PrefixConflictError):
        registry.register(MountSpec(prefix="/api/v1/x", router=APIRouter(), extension="inner"))
    registry.register(
        MountSpec(prefix="/api/v1/y", router=APIRouter(), extension="inner", nests_in=("/api/v1",))
    )
