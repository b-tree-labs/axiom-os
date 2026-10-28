# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A laptop bound to a site node gets that site's tools, documents and skills.

One process plays both installs. The node is the composed serving app, in-process, with its data
read mount and its document search mount behind the real authz hook and a key minted the way the
node mints one. The laptop holds no medallion and no document store: it has a ``site-bindings.json``
naming the node and that key. Its HTTP goes to the node's app; nothing else connects anywhere.

What the laptop must see, through the same MCP surface a harness lists:

* the data tools are listed, and the ones that read data answer from the node, held to the key's
  site; a tool that would read a database directly says it is bound instead of answering for the
  laptop's own database;
* the skills those tools project from are registered, with the agent roster;
* document search returns the community corpus and the site's own, and nothing from another site,
  the node's operator corpus, or a tier above the caller's;
* an install that is not bound says so plainly.

Found when a bound laptop's inventory showed three tools answering from the laptop's own database
(``catalog``, ``activity``, ``describe``), one the node refused (``uncertainty_coverage``, the read
the platform tells every agent to make before quoting a figure), and document search failing with a
JSON parse error where a proxy had refused the key.
"""

from __future__ import annotations

import asyncio
import io
import json
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from axiom.extensions.builtins.data_platform import gold_query as gq
from axiom.extensions.builtins.data_platform import read_api, read_target
from axiom.extensions.builtins.data_platform.services.read_mount import (
    mount_spec as data_read_mount,
)
from axiom.extensions.builtins.http.authz_hook import build_authz_hook, build_bearer_resolver
from axiom.extensions.builtins.http.compose import compose_app
from axiom.extensions.builtins.http.middleware import MiddlewareConfig
from axiom.extensions.builtins.http.mounts import rag_search_mount_spec
from axiom.extensions.builtins.http.registry import RouterRegistry
from axiom.governance import Decision, Verdict
from axiom.rag.store import SearchResult
from axiom.webauth.api_keys import JsonFileApiKeyStore, append_api_key_record, mint_api_key

HOME = "alpha-site"
GUEST = "beta-site"
OTHER = "gamma-site"
FACE = "https://node.example.org"

#: The data reads a bound install must get from its node.
DATA_TOOLS = (
    "axiom_data__roles",
    "axiom_data__series",
    "axiom_data__aggregate",
    "axiom_data__compare",
    "axiom_data__describe",
    "axiom_data__catalog",
    "axiom_data__uncertainty_coverage",
)


# --------------------------------------------------------------------------- the node


def _chunk(path: str, corpus: str, *, tier: str = "public") -> SearchResult:
    return SearchResult(
        source_path=path,
        source_title=path,
        chunk_text=f"retention schedule notes from {path}",
        chunk_index=0,
        similarity=0.0,
        combined_score=0.5,
        corpus=corpus,
        access_tier=tier,
    )


class _SeededStore:
    """The node's document store: a community corpus, the site's own, and what must stay home."""

    def __init__(self) -> None:
        self.chunks = [
            _chunk("community/shared-handbook.md", "rag-community"),
            _chunk("community/methods-reference.pdf", "rag-community"),
            _chunk("site/procedures/startup.md", "rag-org"),
            _chunk("site/logbook/2026-10-24.md", "rag-org"),
            _chunk("site/restricted/design-basis.md", "rag-org", tier="restricted"),
            _chunk("node/operator-transcript.md", "rag-internal"),
            _chunk(f"{GUEST}/own-notes.md", f"rag-tenant:{GUEST}"),
            _chunk(f"{OTHER}/neighbour.md", f"rag-tenant:{OTHER}"),
        ]
        self.asked: list = []

    def search(self, query_embedding=None, query_text="", corpora=None, limit=5, **_):
        self.asked.append(corpora)
        return [c for c in self.chunks if corpora is None or c.corpus in corpora][:limit]


class _FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConnection:
    """The node's medallion. The gold queries are replaced below, so no SQL runs."""

    def set_session(self, **_):
        pass

    def cursor(self):
        return _FakeCursor()

    def close(self):
        pass


def _answered_on_the_node(what: str, site_scope=None) -> dict:
    answered_by = "node" if _SIDE["on_node"] else "laptop"
    return {"data": {"answered_by": answered_by, "what": what, "site_scope": site_scope},
            "provenance": {"tier": "gold", "source": "node", "method": "test"}}


@pytest.fixture
def node(tmp_path, monkeypatch):
    """The composed node app and the key it issued to a colleague at each site."""
    monkeypatch.setenv("AXIOM_SITE", HOME)
    for name in ("AXIOM_HOME_SITES", "AXIOM_FED_DEV_ACCEPT", "DATABASE_URL",
                 "AXIOM_RAG_REMOTE_SIGNATURE", "AXIOM_RAG_REMOTE_NODE_ID"):
        monkeypatch.delenv(name, raising=False)

    keys_file = tmp_path / "node-keys.json"
    issued = {}
    for site, handle in ((HOME, "@reader"), (GUEST, "@reviewer")):
        token, record = mint_api_key(principal=handle, scopes=["*:read"], name="laptop", site=site)
        append_api_key_record(keys_file, record)
        issued[site] = token

    store = _SeededStore()
    registry = RouterRegistry()
    registry.register(data_read_mount())
    registry.register(rag_search_mount_spec(lambda: store, embed_fn=lambda _q: None))
    hook = build_authz_hook(
        resolve_principal=build_bearer_resolver({}, api_keys=JsonFileApiKeyStore(keys_file)),
        decide_fn=lambda _env: Verdict.from_decision(Decision.PERMIT, "test permits", "t-1"),
    )
    app = compose_app(
        registry=registry,
        include_builtins=False,
        middleware=MiddlewareConfig(authz=hook),
        auto_authz=False,
    )

    # The node's gold reads, so each answer says it came from the node and which site it was held to.
    monkeypatch.setattr("psycopg2.connect", lambda *_a, **_k: _FakeConnection())
    monkeypatch.setattr(gq, "roles", lambda cur, **kw: _answered_on_the_node("roles", kw.get("site_scope")))
    monkeypatch.setattr(gq, "list_tables", lambda cur, tier=None: {
        "data": {"tier": "gold", "tables": [{"table": "served_on_the_node" if _SIDE["on_node"] else "laptop"}]},
        "provenance": {"tier": "gold", "source": "gold", "method": "test"},
    })
    monkeypatch.setattr(gq, "describe", lambda cur, table, tier=None: {
        "data": {"table": table, "tier": "gold",
                 "columns": [{"column": "on_the_node" if _SIDE["on_node"] else "laptop", "type": "text"}]},
        "provenance": {"tier": "gold", "source": f"gold.{table}", "method": "test"},
    })
    monkeypatch.setattr(gq, "uncertainty_coverage", lambda cur, **kw: _answered_on_the_node(
        "uncertainty_coverage", kw.get("site_scope")))

    return SimpleNamespace(client=TestClient(app), keys=issued, store=store)


# --------------------------------------------------------------------------- the laptop

#: Which install is running right now. The laptop holds no medallion; the node does. One process
#: plays both, so the answer depends on which side of the HTTP call the code is on.
_SIDE = {"on_node": False}


def _bind(state_dir, site: str) -> None:
    (state_dir / read_target.STATE_FILENAME).write_text(json.dumps({
        "current": site,
        "bindings": {site: {"site": site, "face_url": FACE, "token_ref": f"openbao://kv/data/{site}"}},
    }), encoding="utf-8")


@pytest.fixture
def laptop(node, tmp_path, monkeypatch):
    """A laptop bound to the node with the home site's key. ``bind(site)`` rebinds it."""
    state = tmp_path / "laptop-state"
    state.mkdir()
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    _bind(state, HOME)
    current = {"site": HOME}

    def bind(site: str) -> None:
        _bind(state, site)
        current["site"] = site

    monkeypatch.setattr(read_target, "holds_medallion", lambda: _SIDE["on_node"])
    monkeypatch.setattr(read_api, "holds_medallion", lambda: _SIDE["on_node"])
    # The key the binding names, as the vault would hand it back.
    monkeypatch.setattr(read_target, "_key", lambda _face: node.keys[current["site"]])

    def _to_node(method_post):
        _SIDE["on_node"] = True
        try:
            return method_post()
        finally:
            _SIDE["on_node"] = False

    class _Response:
        def __init__(self, raw: bytes) -> None:
            self._raw = raw

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self) -> bytes:
            return self._raw

    def urlopen(request, timeout=None):  # the data read's transport
        assert request.full_url.startswith(FACE), request.full_url
        response = _to_node(lambda: node.client.post(
            request.full_url[len(FACE):], content=request.data, headers=dict(request.header_items())))
        if response.status_code >= 400:
            raise urllib.error.HTTPError(request.full_url, response.status_code, "refused", {}, io.BytesIO())
        return _Response(response.content)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    def requests_post(url, json=None, headers=None, timeout=None):  # document search's transport
        assert url.startswith(FACE), url
        return _to_node(lambda: node.client.post(url[len(FACE):], json=json, headers=headers or {}))

    from axiom.rag import remote_store

    monkeypatch.setattr(remote_store.requests, "post", requests_post)
    return SimpleNamespace(state=state, bind=bind)


@pytest.fixture(scope="module")
def surface():
    """The MCP surface a harness lists on this install (built once; it does not read the binding)."""
    from axiom.extensions.builtins.mcp.aggregation import AggregationRegistry

    return AggregationRegistry.from_node().build()


def _call(surface, tool: str, **args):
    result = asyncio.run(surface.dispatch[tool](args))
    return result if isinstance(result, dict) else json.loads(result)


def _paths(result: dict) -> set[str]:
    return {r["source_path"] for r in result.get("results", [])}


# --------------------------------------------------------------------------- tools


def test_the_bound_install_lists_the_sites_data_and_document_tools(surface):
    names = {t.name for t in surface.tools}
    missing = [t for t in (*DATA_TOOLS, "axiom_rag__retrieve", "axiom_agents__roster") if t not in names]
    assert not missing, f"not on the MCP surface: {missing}"


def test_a_data_read_answers_from_the_node_held_to_the_keys_site(surface, laptop):
    roles = _call(surface, "axiom_data__roles")
    assert roles["ok"], roles
    assert roles["value"]["data"]["answered_by"] == "node"
    assert roles["value"]["data"]["site_scope"] is None  # a home-site key reads the home site

    laptop.bind(GUEST)
    guest = _call(surface, "axiom_data__roles")
    assert guest["ok"], guest
    assert guest["value"]["data"]["site_scope"] == GUEST  # held to the key's site, on the node


def test_uncertainty_coverage_follows_the_binding(surface, laptop):
    """The read every agent is told to make before quoting a figure; the node refused it (HTTP 400)."""
    result = _call(surface, "axiom_data__uncertainty_coverage")
    assert result["ok"], result
    assert result["value"]["data"]["answered_by"] == "node"


def test_the_gold_catalog_and_a_tables_shape_come_from_the_node(surface, laptop):
    catalog = _call(surface, "axiom_data__catalog")
    assert catalog["ok"], catalog
    assert catalog["value"]["data"]["objects"] == [{"table": "served_on_the_node"}]
    assert HOME in catalog["value"]["provenance"]["source"]

    shape = _call(surface, "axiom_data__describe", object="anything")
    assert shape["ok"], shape
    assert shape["value"]["data"]["columns"] == [{"column": "on_the_node", "type": "text"}]


@pytest.mark.parametrize(
    "tool, args",
    [
        ("axiom_data__activity", {}),
        ("axiom_data__freshness", {}),
        ("axiom_data__catalog", {"tier": "silver"}),
        ("axiom_data__tier_audit", {}),
        ("axiom_data__gaps", {"site": HOME}),
    ],
)
def test_a_read_that_cannot_follow_the_binding_says_so_instead_of_answering_for_the_laptop(
    surface, laptop, monkeypatch, tool, args
):
    opened = []
    monkeypatch.setattr("psycopg2.connect", lambda *a, **k: opened.append(a) or _FakeConnection())
    result = _call(surface, tool, **args)
    assert not result["ok"], result
    text = " ".join(result["errors"])
    assert "bound to" in text and HOME in text and FACE in text, text
    assert opened == []  # never opened a database on the laptop


# --------------------------------------------------------------------------- skills and agents


def test_the_skills_behind_the_tools_are_registered_and_projected(surface):
    from axiom.extensions.builtins.data_platform.skills import bind
    from axiom.extensions.builtins.mcp.skill_tools import is_mcp_exposed
    from axiom.infra.skills import SkillRegistry

    registry = SkillRegistry()
    bind(registry)
    specs = registry.specs()
    for verb in read_target.FOLLOWS_THE_BINDING:
        if verb == "data.tables":  # served to bound installs; on MCP as `catalog`
            continue
        assert verb in specs and is_mcp_exposed(specs[verb]), f"{verb} is not projected onto MCP"

    roster = _call(surface, "axiom_agents__roster")
    assert roster["ok"] and roster["value"]["agents"], roster


def test_every_read_the_laptop_forwards_is_one_the_node_serves():
    assert set(read_api._VERBS) == set(read_target.FOLLOWS_THE_BINDING)


# --------------------------------------------------------------------------- documents


def test_document_search_returns_the_community_corpus_and_the_sites_own(surface, laptop, node):
    result = _call(surface, "axiom_rag__retrieve", query="retention schedule", k=20)
    assert result["ok"], result
    paths = _paths(result)
    assert {"community/shared-handbook.md", "community/methods-reference.pdf"} <= paths
    assert {"site/procedures/startup.md", "site/logbook/2026-10-24.md"} <= paths
    assert "site/restricted/design-basis.md" not in paths  # above the caller's tier
    assert "node/operator-transcript.md" not in paths  # the node's operator corpus
    assert not any(p.startswith((f"{GUEST}/", f"{OTHER}/")) for p in paths)  # other sites
    assert node.store.asked, "the search ran on the node's store"


def test_a_guest_sites_key_gets_the_community_corpus_and_its_own_only(surface, laptop):
    laptop.bind(GUEST)
    paths = _paths(_call(surface, "axiom_rag__retrieve", query="retention schedule", k=20))
    assert "community/shared-handbook.md" in paths and f"{GUEST}/own-notes.md" in paths
    assert not any(p.startswith("site/") for p in paths)  # the home site's material stays home
    assert f"{OTHER}/neighbour.md" not in paths


def test_a_refused_key_is_reported_as_a_refusal_not_a_parse_error(surface, laptop, monkeypatch):
    """A proxy in front of the node answered 401 with an HTML page; the laptop said JSONDecodeError."""
    from axiom.rag import remote_store

    page = SimpleNamespace(status_code=401, content=b"<html>401</html>", text="<html>401</html>",
                           json=lambda: json.loads("<html>"))
    monkeypatch.setattr(remote_store.requests, "post", lambda *a, **k: page)
    result = _call(surface, "axiom_rag__retrieve", query="retention schedule")
    assert not result["ok"]
    assert "HTTP 401" in result["error"] and "refused" in result["error"], result["error"]
    assert "JSONDecodeError" not in result["error"]


# --------------------------------------------------------------------------- unbound


def test_an_unbound_install_says_so_plainly(surface, tmp_path, monkeypatch):
    state = tmp_path / "unbound-state"
    state.mkdir()
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(
        "axiom.extensions.builtins.settings.store.SettingsStore",
        lambda: SimpleNamespace(get=lambda _k, default=None: default),
    )
    monkeypatch.setattr(read_target, "holds_medallion", lambda: False)

    target = read_target.resolve_read_target()
    assert target.where == "medallion" and "no binding" in target.reason

    result = _call(surface, "axiom_rag__retrieve", query="retention schedule")
    assert not result["ok"]
    assert "not bound to a site" in result["note"], result
