# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A node's database volume is watched like its own, and its alarm reaches
every place a person looks: the node's health check, its status, and the
platform's view of the site.

A database whose volume fills stops (its write-ahead log has nowhere to go),
and watching only the volume where ingest lands never sees it coming when the
database sits on another volume. The alarm floor (5 GiB or 10% free unless
set) comes well before the volume is full.
"""

from __future__ import annotations

import time

import pytest

from axiom.extensions.builtins.data_platform import partner_health as ph
from axiom.extensions.builtins.data_platform.forward.forwarder import _disk_beat
from axiom.extensions.builtins.data_platform.ingest_sink import headroom as hr
from axiom.extensions.builtins.data_platform.ingest_sink.heartbeat import HeartbeatStore
from axiom.extensions.builtins.status import function_status as fs


@pytest.fixture
def database_volume(tmp_path, monkeypatch):
    vol = tmp_path / "database"
    vol.mkdir()
    monkeypatch.setenv(hr.WATCH_ENV, f"database={vol}")
    return vol


def _filling(monkeypatch):
    # Every real disk is "below" a floor of 100% free: the volume is filling.
    monkeypatch.setenv(hr.ALARM_FREE_PERCENT_ENV, "100")


def _roomy(monkeypatch):
    monkeypatch.setenv(hr.ALARM_FREE_BYTES_ENV, "1")
    monkeypatch.setenv(hr.ALARM_FREE_PERCENT_ENV, "0")
    monkeypatch.setenv(hr.MIN_FREE_BYTES_ENV, "1")
    monkeypatch.setenv(hr.MIN_FREE_PERCENT_ENV, "0")


def test_a_watched_volume_is_measured_and_labelled(database_volume, monkeypatch):
    _roomy(monkeypatch)
    (d,) = hr.watched()
    assert d["label"] == "database" and d["path"] == str(database_volume)
    assert d["total_bytes"] > 0 and d["alarm"] is False and d["ok"] is True
    _filling(monkeypatch)
    assert hr.watched()[0]["alarm"] is True


def test_a_watch_whose_volume_is_not_mounted_is_an_alarm_not_a_silence(tmp_path, monkeypatch):
    monkeypatch.setenv(hr.WATCH_ENV, f"database={tmp_path / 'not-mounted'}")
    (d,) = hr.watched()
    assert d["alarm"] is True and d["error"] == "not mounted here"


def test_the_health_check_reports_the_database_volume(database_volume, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.data_platform.ingest_sink.edge import build_health_router

    monkeypatch.setenv("AXI_STATE_DIR", str(database_volume.parent))
    app = FastAPI()
    app.include_router(build_health_router())
    _filling(monkeypatch)
    body = TestClient(app).get("/healthz").json()
    # Still answering (there is room to act); the alarm is said, not acted on.
    assert body["status"] == "ok" and body["disk_alarm"] is True
    assert [d["label"] for d in body["disks"]] == ["database"]


def test_status_turns_amber_at_the_alarm_not_at_full(database_volume, monkeypatch):
    # The host's real disk may itself be over the share thresholds (a CI
    # runner often is); this test is about the alarm floor, so lift them.
    monkeypatch.setattr(fs, "DISK_WARN", 1.01)
    monkeypatch.setattr(fs, "DISK_FAIL", 1.01)
    _roomy(monkeypatch)
    (row,) = fs._archive(None)["rows"]
    assert row["label"] == "database disk" and row["state"] == "ok"
    _filling(monkeypatch)
    (row,) = fs._archive(None)["rows"]
    assert row["state"] == "warn" and "database volume" in row["fix"]


def test_the_heartbeat_carries_it_and_the_platform_alerts_on_it(database_volume, monkeypatch, tmp_path):
    _filling(monkeypatch)
    beat = _disk_beat({})
    assert beat["disk_alarm"] is True and [d["label"] for d in beat["disks"]] == ["database"]
    store = HeartbeatStore(tmp_path / "hb")
    # Mostly empty by share, yet below the node's own floor: still an alert.
    store.record("site-a", {"node": "daq-pc", **beat, "disk_used": 0.30}, received_at=time.time())
    h = ph.site_health("site-a", store=store, now=time.time())
    disk = h["nodes"][0]["checks"]["disk"]
    assert disk["state"] == "warn" and "database" in disk["text"]
    assert "disk_high" in [a["kind"] for a in ph.alerts(h, now=time.time())]
