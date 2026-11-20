# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The face serves a data read only when this install holds the medallion."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.data_platform import read_api
from axiom.infra.skills import SkillResult


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(read_api.build_data_read_router())
    return TestClient(app)


def test_a_node_without_the_medallion_does_not_serve_the_read(monkeypatch):
    monkeypatch.setattr(read_api, "holds_medallion", lambda: False)
    response = _client().post("/data/query", json={"verb": "data.tables", "params": {}})
    assert response.status_code == 404


def test_a_node_that_holds_the_medallion_runs_the_verb(monkeypatch):
    monkeypatch.setattr(read_api, "holds_medallion", lambda: True)
    monkeypatch.setattr(
        read_api.gold,
        "tables",
        lambda params, ctx: SkillResult(ok=True, value=[{"table": "signals"}]),
    )
    response = _client().post("/data/query", json={"verb": "data.tables", "params": {}})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["value"] == [{"table": "signals"}]


def test_an_unknown_verb_is_refused(monkeypatch):
    monkeypatch.setattr(read_api, "holds_medallion", lambda: True)
    response = _client().post("/data/query", json={"verb": "data.kit_down", "params": {}})
    assert response.status_code == 400
