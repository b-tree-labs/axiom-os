# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A node that sends data says it is alive over the same path, and is seen.

A partner installs a collector and asks the only question that matters to
them: is it working? Rows landing answer it only while there is data to send;
a collector between runs looks exactly like a dead one. A heartbeat is a small
typed record on the same authenticated path, kept per site and node. It is
not sensor data, so it never enters bronze and is never conformed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from axiom.extensions.builtins.data_platform.ingest_sink import heartbeat as hb
from axiom.extensions.builtins.data_platform.ingest_sink.api import build_tabular_ingest_router
from axiom.extensions.builtins.data_platform.ingest_sink.tabular import TabularIngestSink
from axiom.extensions.builtins.data_platform.ingest_sink.tenancy import TenancyPolicy


def _beat(node="daq-pc-1", **extra):
    return {
        "node": node,
        "sent_at": "2026-10-07T03:00:00Z",
        "collector": "running",
        "last_reading_at": "2026-10-07T02:59:58Z",
        "readings_today": 1200,
        "pending": 0,
        "route": [{"hop": "collector", "state": "ok"}],
        **extra,
    }


@pytest.fixture
def store(tmp_path: Path) -> hb.HeartbeatStore:
    return hb.HeartbeatStore(tmp_path / "hb")


def test_the_latest_beat_per_node_is_kept_and_history_is_bounded(store):
    for i in range(hb.HISTORY + 5):
        store.record("site-a", _beat(readings_today=i))
    nodes = store.nodes("site-a")
    assert [n["node"] for n in nodes] == ["daq-pc-1"]
    assert nodes[0]["latest"]["readings_today"] == hb.HISTORY + 4
    assert len(store.history("site-a", "daq-pc-1")) == hb.HISTORY


def test_one_site_never_sees_another_sites_nodes(store):
    store.record("site-a", _beat())
    store.record("site-b", _beat(node="other"))
    assert [n["node"] for n in store.nodes("site-a")] == ["daq-pc-1"]
    assert store.nodes("site-c") == []


def test_a_node_name_cannot_walk_out_of_its_site_directory(store):
    with pytest.raises(ValueError):
        store.record("site-a", _beat(node="../site-b/x"))


def test_a_beat_missing_its_node_is_refused(store):
    with pytest.raises(ValueError):
        store.record("site-a", {"sent_at": "x"})


def test_liveness_is_judged_from_the_time_it_was_received(store):
    store.record("site-a", _beat())
    (n,) = store.nodes("site-a")
    assert hb.liveness(n, now=n["received_at"] + 60) == "online"
    assert hb.liveness(n, now=n["received_at"] + hb.ONLINE_WITHIN_S + 1) == "late"
    assert hb.liveness(n, now=n["received_at"] + hb.OFFLINE_AFTER_S + 1) == "offline"


def test_a_heartbeat_batch_on_the_row_path_is_kept_and_never_written_as_rows(tmp_path, monkeypatch):
    monkeypatch.setenv(hb.DIR_ENV, str(tmp_path / "hb"))

    class NeverWrites:
        def write(self, *a, **k):  # pragma: no cover - must not be reached
            raise AssertionError("a heartbeat reached the bronze writer")

    app = FastAPI()
    app.include_router(
        build_tabular_ingest_router(
            TabularIngestSink(writer=NeverWrites()),  # type: ignore[arg-type]
            tenancy=TenancyPolicy(),
        )
    )
    resp = TestClient(app).post(
        "/ingest/rows",
        json={
            "source": "site-a-push",
            "batches": [
                {"item_id": "hb-1", "schema_ref": hb.HEARTBEAT_SCHEMA, "rows": [_beat()]}
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["heartbeats"] == 1
    assert resp.json()["rows_in"] == 0
    assert hb.HeartbeatStore.from_env().nodes("<unattributed>")[0]["node"] == "daq-pc-1"


def test_the_sender_can_ask_whether_its_beat_arrived(tmp_path, monkeypatch):
    monkeypatch.setenv(hb.DIR_ENV, str(tmp_path / "hb"))

    class NeverWrites:
        def write(self, *a, **k):  # pragma: no cover
            raise AssertionError

    app = FastAPI()
    app.include_router(
        build_tabular_ingest_router(TabularIngestSink(writer=NeverWrites()), tenancy=TenancyPolicy())  # type: ignore[arg-type]
    )
    c = TestClient(app)
    assert c.get("/ingest/heartbeat").json()["nodes"] == []
    c.post("/ingest/rows", json={"source": "s", "batches": [
        {"item_id": "hb", "schema_ref": hb.HEARTBEAT_SCHEMA, "rows": [_beat()]}]})
    (node,) = c.get("/ingest/heartbeat").json()["nodes"]
    assert node["node"] == "daq-pc-1" and node["state"] == "online"
