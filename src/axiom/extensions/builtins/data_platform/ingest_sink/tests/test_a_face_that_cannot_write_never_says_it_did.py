# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A face that cannot write answers with an error, not a 200.

Measured 2026-10-08 on a node whose disk filled: 389 pushes were each answered
200 while only 67 batches landed. The failure was reported only inside the
body as ``errored``. The collector reads that field; a producer that trusts the
status code advances past data that never landed. A push in which nothing
could be written is now 507, and the health check says the node cannot write.
"""

from __future__ import annotations

import os

import pytest

from ...ingest_sink import TabularIngestSink
from .test_tabular_api import _batch, _client, _writer


@pytest.fixture
def unwritable(tmp_path):
    if os.name == "nt" or os.geteuid() == 0:
        pytest.skip("needs a POSIX permission the test user cannot override")
    bronze = tmp_path / "bronze"
    bronze.mkdir()
    bronze.chmod(0o500)  # a real write failure from the filesystem, like a full disk
    yield tmp_path
    bronze.chmod(0o700)


def test_a_push_where_nothing_could_be_written_is_507(unwritable):
    client = _client(sink=TabularIngestSink(writer=_writer(unwritable)))
    resp = client.post("/ingest/rows", json={"source": "unit-src", "batches": [_batch()]})
    assert resp.status_code == 507
    assert "could not be written" in resp.text


def test_a_push_that_wrote_something_is_still_200_with_errored_counted(tmp_path):
    client = _client(sink=TabularIngestSink(writer=_writer(tmp_path)))
    resp = client.post("/ingest/rows", json={"source": "unit-src", "batches": [_batch()]})
    assert resp.status_code == 200 and resp.json()["errored"] == 0


def test_health_says_the_node_cannot_write(unwritable, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from ...ingest_sink.edge import build_health_router

    monkeypatch.setenv("AXIOM_INGEST_OUTBOX_DIR", str(unwritable / "bronze"))
    app = FastAPI()
    app.include_router(build_health_router())
    resp = TestClient(app).get("/healthz")
    assert resp.status_code == 503
    assert resp.json()["status"] == "cannot_write"


def test_health_is_ok_when_it_can_write(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from ...ingest_sink.edge import build_health_router

    monkeypatch.setenv("AXIOM_INGEST_OUTBOX_DIR", str(tmp_path))
    app = FastAPI()
    app.include_router(build_health_router())
    resp = TestClient(app).get("/healthz")
    assert resp.status_code == 200 and resp.json()["status"] == "ok"
    assert resp.json()["free_bytes"] > 0
