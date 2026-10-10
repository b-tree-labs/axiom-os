# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A site's page shows the nodes that send its data, and the path to the page.

Bounded like every other site route: a site outside the deployment answers
404, so the existence of another tenant's collector is not disclosed.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.data_platform.ingest_sink.heartbeat import DIR_ENV, HeartbeatStore
from axiom.extensions.builtins.webapp.api.catalog import register_catalog_routes
from axiom.infra.site_scope import SERVED_SITES_ENV


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(DIR_ENV, str(tmp_path / "hb"))
    monkeypatch.setenv(SERVED_SITES_ENV, "site-a")
    monkeypatch.setenv("AXIOM_NODE_NAME", "host-1")
    planned = tmp_path / "topo.toml"
    planned.write_text('[[site."site-a".planned]]\nname = "ingest edge"\nwhere = "not provisioned yet"\n')
    monkeypatch.setenv("AXIOM_SITE_TOPOLOGY", str(planned))
    router = APIRouter()
    register_catalog_routes(router)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_before_any_collector_reports_the_page_draws_the_plan(client):
    body = client.get("/sites/site-a/topology").json()
    states = {h["name"]: h["state"] for h in body["hops"]}
    assert states == {"collector": "planned", "ingest edge": "planned",
                      "data platform": "planned", "web app": "ok"}
    assert "not installed yet" in body["caption"]


def test_a_reporting_collector_is_online_on_its_site_page(client, tmp_path):
    HeartbeatStore(tmp_path / "hb").record("site-a", {"node": "daq-pc-1", "collector": "running"})
    nodes = client.get("/sites/site-a/nodes").json()["nodes"]
    assert [(n["node"], n["state"]) for n in nodes] == [("daq-pc-1", "online")]
    hops = client.get("/sites/site-a/topology").json()["hops"]
    assert hops[0]["name"] == "daq-pc-1" and hops[0]["state"] == "online"


def test_another_sites_nodes_are_not_disclosed(client, tmp_path):
    HeartbeatStore(tmp_path / "hb").record("site-b", {"node": "theirs"})
    assert client.get("/sites/site-b/nodes").status_code == 404
    assert client.get("/sites/site-b/topology").status_code == 404


def test_a_site_health_answers_for_its_own_site_only(client, tmp_path):
    store = HeartbeatStore(tmp_path / "hb")
    store.record("site-a", {"node": "daq-pc-1", "collector": "running", "version": "1.17.1", "disk_used": 0.9})
    store.record("site-b", {"node": "theirs", "version": "1.0.0"})
    body = client.get("/sites/site-a/health").json()
    assert body["state"] == "warn" and body["nodes"][0]["checks"]["disk"]["state"] == "warn"
    assert client.get("/sites/site-b/health").status_code == 404
    listed = [h["site"] for h in client.get("/health/sites").json()["sites"]]
    assert listed == ["site-a"]


def test_a_sites_uptime_is_scoped_and_names_its_missing_evidence(client, tmp_path):
    HeartbeatStore(tmp_path / "hb").record("site-a", {"node": "daq-pc-1"})
    body = client.get("/sites/site-a/uptime").json()
    assert body["nodes"] == ["daq-pc-1"] and body["evidence_missing"]
    assert client.get("/sites/site-b/uptime").status_code == 404
