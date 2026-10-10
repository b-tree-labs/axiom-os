# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""An intake declares the oldest sender it accepts; a sender older than that is told, and loses nothing.

A node left alone for months comes back older than the platform supports. It
must not be dead-lettered into silence (a 4xx the transmitter treats as
definitive throws the rows away), and it must not keep pushing data an intake
can no longer read correctly. So the intake answers 426 with a body that says
what is required and the command that gets there, and the transmitter holds:
rows stay in its journal, the link says "update required", and once the node
is updated everything it held is sent.

Real router, real bronze sink, real transmitter and journal. The only adapter
is a transport that hands the transmitter's request to the in-process app.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from axiom.extensions.builtins.data_platform import compat
from axiom.extensions.builtins.data_platform.bronze import (
    FilesystemTabularBronzeSink,
    TabularBronzeWriter,
)
from axiom.extensions.builtins.data_platform.daq.consolidator import DAQConsolidator
from axiom.extensions.builtins.data_platform.daq.envelope import ConsolidatedRecord
from axiom.extensions.builtins.data_platform.daq.journal import DAQJournal
from axiom.extensions.builtins.data_platform.daq.transmitter import DAQTransmitter
from axiom.extensions.builtins.data_platform.ingest_sink import TabularIngestSink
from axiom.extensions.builtins.data_platform.ingest_sink import heartbeat as hb
from axiom.extensions.builtins.data_platform.ingest_sink.api import build_tabular_ingest_router
from axiom.extensions.builtins.http.server import create_app
from axiom.rag.ingest_router import Disposition


class _InProcess:
    """Hands the transmitter's POST to the app in this process."""

    def __init__(self, client: TestClient):
        self.client = client

    def post(self, url, body, headers):
        r = self.client.post(url.replace("http://intake.invalid", ""), content=body, headers=headers)
        return r.status_code, r.text, dict(r.headers)


def _app(tmp_path, monkeypatch, minimum: str | None):
    monkeypatch.setenv(hb.DIR_ENV, str(tmp_path / "hb"))
    if minimum:
        monkeypatch.setenv(compat.MIN_CLIENT_ENV, minimum)
    else:
        monkeypatch.delenv(compat.MIN_CLIENT_ENV, raising=False)
    writer = TabularBronzeWriter(rules=[], sink=FilesystemTabularBronzeSink(root=tmp_path / "bronze"),
                                 default_disposition=Disposition.ALLOW, default_tier="rag-org")
    app = create_app(title="t", version="0", description="")
    app.include_router(build_tabular_ingest_router(sink=TabularIngestSink(writer=writer)))
    return TestClient(app)


def _journal(tmp_path, n=5):
    j = DAQJournal(tmp_path / "journal")
    cons = DAQConsolidator(journal=j, producer_id="site-b", feed="loop")
    for i in range(n):
        cons.consume(ConsolidatedRecord(schema_id="site-b/epics-v1", ts=f"2026-10-08T00:00:{i:02d}Z", values={"STC1": 1.0 + i}))
    return j


def _tx(journal, client, versions):
    return DAQTransmitter(face_url="http://intake.invalid", source="site-b-push", schema_ref="site-b/epics-v1",
                          token=None, transport=_InProcess(client), journal=journal, jitter=None,
                          client_versions=versions)


# -- the rule itself ---------------------------------------------------------------------


def test_a_requirement_is_checked_against_what_the_sender_says_it_runs():
    req = "collector-pkg>=1.17.1"
    assert compat.check("collector-pkg/1.17.1 axiom-os-lm/0.67.5", req) is None
    assert compat.check("collector-pkg/1.18.0", req) is None
    too_old = compat.check("collector-pkg/1.16.0 axiom-os-lm/0.67.0", req)
    assert too_old["code"] == "update_required"
    assert too_old["running"] == "collector-pkg/1.16.0"
    assert "collector-pkg>=1.17.1" in too_old["command"]


def test_a_sender_that_says_nothing_is_older_than_any_declared_minimum():
    assert compat.check("", "collector-pkg>=1.17.1")["running"] is None


def test_no_declared_minimum_accepts_everyone():
    assert compat.check("", "") is None


# -- end to end ------------------------------------------------------------------------


def test_an_old_sender_holds_its_rows_and_says_update_required(tmp_path, monkeypatch):
    client = _app(tmp_path, monkeypatch, "collector-pkg>=1.17.1")
    j = _journal(tmp_path)
    tx = _tx(j, client, "collector-pkg/1.16.0")
    result = tx.pump()
    assert result.last_status == 426
    assert result.sent == 0 and result.refused == 0
    assert j.lag(tx.cursor_name) == 5  # nothing dead-lettered, nothing lost
    health = tx.health_details()
    assert health["connection"] == "update_required"
    assert "collector-pkg>=1.17.1" in health["update_required"]["command"]
    assert not tx.deadletter_path.exists()
    assert not list((tmp_path / "bronze").rglob("*.jsonl"))  # the intake wrote nothing


def test_after_updating_everything_held_is_sent(tmp_path, monkeypatch):
    client = _app(tmp_path, monkeypatch, "collector-pkg>=1.17.1")
    j = _journal(tmp_path)
    old = _tx(j, client, "collector-pkg/1.16.0")
    old.pump()
    # The node is updated: same journal, a newer sender.
    new = _tx(j, client, "collector-pkg/1.17.1")
    while j.lag(new.cursor_name):
        r = new.pump()
        assert r.last_status == 200, r.detail
    assert new.sent_total == 5
    assert new.health_details()["update_required"] is None


def test_the_hold_does_not_escalate_backoff_or_hammer(tmp_path, monkeypatch):
    client = _app(tmp_path, monkeypatch, "collector-pkg>=1.17.1")
    j = _journal(tmp_path)
    tx = _tx(j, client, "collector-pkg/1.16.0")
    r = tx.pump()
    assert r.retry_after_s >= compat.UPDATE_RECHECK_S
    assert tx.health_details()["consecutive_failures"] == 0


def test_an_old_sender_is_still_heard_by_its_heartbeat(tmp_path, monkeypatch):
    client = _app(tmp_path, monkeypatch, "collector-pkg>=1.17.1")
    beat = {"node": "daq-pc-1", "sent_at": "2026-10-08T00:00:00Z", "collector": "running",
            "versions": "collector-pkg/1.16.0"}
    r = client.post("/ingest/rows", headers={compat.HEADER: "collector-pkg/1.16.0"},
                    json={"source": "site-b-push", "batches": [
                        {"item_id": "hb", "schema_ref": hb.HEARTBEAT_SCHEMA, "rows": [beat]}]})
    assert r.status_code == 200, r.text
    assert r.json()["update_required"]["code"] == "update_required"
    (node,) = client.get("/ingest/heartbeat").json()["nodes"]
    assert node["node"] == "daq-pc-1"


def test_a_current_sender_is_unaffected(tmp_path, monkeypatch):
    client = _app(tmp_path, monkeypatch, None)
    j = _journal(tmp_path)
    tx = _tx(j, client, "collector-pkg/1.16.0")
    assert tx.pump().last_status == 200


def test_the_transmitter_says_what_it_runs_by_default(tmp_path):
    j = _journal(tmp_path, n=1)
    seen = {}

    class Face:
        def post(self, url, body, headers):
            seen.update(headers)
            return 200, json.dumps({"errored": 0}), {}

    DAQTransmitter(face_url="http://intake.invalid", source="s", schema_ref="x", token=None,
                   transport=Face(), journal=j, jitter=None).pump()
    assert "axiom-os-lm/" in seen[compat.HEADER]


def test_every_sender_can_import_the_window_without_extra_packages():
    """Every transmitter imports this; an undeclared dependency would crash every collector."""
    import subprocess
    import sys

    code = "import sys; sys.modules['packaging'] = None; from axiom.extensions.builtins.data_platform import compat; print(compat.check('a/1.2', 'a>=1.1'))"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "None", r.stderr
