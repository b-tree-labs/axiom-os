# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""An edge can share a host with an existing site, behind that host's nginx.

The first deployment puts the edge on a VM that already serves another site
(ADR-177). The snippet added to that site's server block may only add the
edge's paths, must forward to the loopback edge, must bound the body before
Python sees it, and must not let a client choose the address the edge sees.
Behind it, no forwarded header can stand in for a credential.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy" / "ingest-edge"
SNIPPET = DEPLOY / "nginx-axiom-edge.conf"


def _directives(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def test_the_snippet_is_one_block_for_the_edges_paths():
    text = SNIPPET.read_text(encoding="utf-8")
    locations = re.findall(r"^\s*location\s+([^{]+?)\s*\{", text, re.M)
    assert locations == ["~ ^/(ingest|edge)(/|$)"]
    # A snippet included into someone else's server block must not open its own.
    assert not re.search(r"^\s*server\s*\{", text, re.M)
    assert "listen" not in " ".join(_directives(text))
    assert re.search(r"proxy_pass\s+http://127\.0\.0\.1:8787;", text)


def test_the_block_matches_only_the_edges_paths():
    pattern = re.compile(r"^/(ingest|edge)(/|$)")
    for path in ("/ingest", "/ingest/rows", "/ingest/healthz", "/edge/outbox"):
        assert pattern.match(path), path
    for path in ("/", "/elog/", "/shadow/superset/", "/ingestion", "/edges", "/healthz"):
        assert not pattern.match(path), path


def test_the_body_is_bounded_like_the_caddy_variant():
    text = SNIPPET.read_text(encoding="utf-8")
    caddy = (DEPLOY / "Caddyfile").read_text(encoding="utf-8")
    assert re.search(r"max_size\s+64MB", caddy)
    assert re.search(r"client_max_body_size\s+64m;", text)


def test_a_client_cannot_choose_the_address_the_edge_sees():
    text = SNIPPET.read_text(encoding="utf-8")
    # Overwrite, never append: $proxy_add_x_forwarded_for keeps a client's own header.
    assert "$proxy_add_x_forwarded_for" not in text
    assert re.search(r"proxy_set_header\s+X-Forwarded-For\s+\$remote_addr;", text)
    assert re.search(r"proxy_set_header\s+X-Real-IP\s+\$remote_addr;", text)


@pytest.fixture
def edge_client(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.http.compose import compose_app
    from axiom.extensions.builtins.http.registry import RouterRegistry
    from axiom.webauth import append_api_key_record, mint_api_key

    from ...agents.plinth.connectors import ConnectorConfig, save_connector

    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "st"))
    monkeypatch.setenv("AXIOM_INGEST_OUTBOX_DIR", str(tmp_path / "ob"))
    monkeypatch.setenv("AXIOM_GATE_API_KEYS_FILE", str(tmp_path / "keys.json"))
    for var in ("AXIOM_MODE", "AXIOM_API_KEY", "AXIOM_HTTP_API_KEYS", "AXIOM_SERVE_INSECURE"):
        monkeypatch.delenv(var, raising=False)
    token, rec = mint_api_key(principal="@daq:site-a", scopes=("data_platform:invoke",), site="site-a")
    append_api_key_record(tmp_path / "keys.json", rec)
    save_connector(ConnectorConfig(name="src", kind="push", bronze_root=str(tmp_path / "b"), site="site-a",
                                   default_disposition="allow", default_tier="restricted"),
                   state_dir=tmp_path / "st")
    app = compose_app(profile="ingest-edge", registry=RouterRegistry(), bind_host="127.0.0.1")
    return TestClient(app), token


BODY = {"source": "src", "batches": [{"item_id": "x", "schema_ref": "s", "rows": [{"a": 1}]}]}


@pytest.mark.parametrize("forwarded", ["203.0.113.9", "127.0.0.1", "::1"])
def test_no_forwarded_address_stands_in_for_a_credential(edge_client, forwarded):
    client, _ = edge_client
    headers = {"X-Forwarded-For": forwarded, "X-Real-IP": forwarded, "X-Forwarded-Proto": "https"}
    assert client.post("/ingest/rows", json=BODY, headers=headers).status_code in (401, 403)


def test_a_forwarded_request_with_a_key_lands(edge_client):
    client, token = edge_client
    headers = {"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https",
               "Authorization": f"Bearer {token}"}
    assert client.post("/ingest/rows", json=BODY, headers=headers).status_code == 200


def test_an_anonymous_push_is_refused_by_the_edge_not_the_proxy(edge_client):
    # The public check that the route reaches the edge: the edge's own 401/403
    # envelope, where a broken route gives nginx's 404 or 502.
    client, _ = edge_client
    r = client.post("/ingest/rows", json=BODY)
    assert r.status_code in (401, 403)
    assert r.headers["content-type"].startswith("application/json")
