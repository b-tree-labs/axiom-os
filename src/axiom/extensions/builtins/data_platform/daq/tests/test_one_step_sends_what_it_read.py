# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""One producer step sends everything it has, not one batch.

``pump()`` sends one request and a step called it once, so sending was capped at
``batch_size`` per step (200 a second at the default interval) however far
behind the journal was. A step now pumps until the journal is caught up, the
face asks it to wait, or a time budget is spent, and appends a read's readings
with one fsync (``journal.batch()``).

The face here is the real ingest router and bronze writer, in process.
"""

from __future__ import annotations

import time

import pytest

from axiom.extensions.builtins.data_platform.daq import ConsolidatedRecord, DAQJournal
from axiom.extensions.builtins.data_platform.daq.consolidator import DAQConsolidator
from axiom.extensions.builtins.data_platform.daq.producer import Producer
from axiom.extensions.builtins.data_platform.daq.transmitter import DAQTransmitter


class _Reader:
    def __init__(self, n):
        self.n = n
        self.done = False

    def read(self):
        if self.done:
            return []
        self.done = True
        return [("f", ConsolidatedRecord(schema_id="s/v1", ts=f"2026-10-08T10:00:00.{i:06d}+00:00",
                                         values={"CH": float(i)}, tags={}, quality="good"))
                for i in range(self.n)]


class _RealFace:
    """The real ingest router over a real bronze writer, called in process."""

    def __init__(self, tmp_path):
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from axiom.extensions.builtins.http.server import create_app
        from axiom.rag.ingest_router import Disposition

        from ...bronze import FilesystemTabularBronzeSink, TabularBronzeWriter
        from ...ingest_sink import TabularIngestSink
        from ...ingest_sink.api import build_tabular_ingest_router

        writer = TabularBronzeWriter(rules=[], sink=FilesystemTabularBronzeSink(root=tmp_path / "bronze"),
                                     default_disposition=Disposition.ALLOW, default_tier="rag-org")
        app = create_app(title="t", version="0", description="")
        app.include_router(build_tabular_ingest_router(sink=TabularIngestSink(writer=writer)))
        self.client = TestClient(app)
        self.requests = 0
        self.rows = 0

    def post(self, url, body, headers):
        self.requests += 1
        r = self.client.post("/ingest/rows", content=body, headers=headers)
        if r.status_code == 200:
            self.rows += r.json().get("rows_landed", 0)
        return r.status_code, r.text


def _producer(tmp_path, face, n, **kw):
    j = DAQJournal(tmp_path / "j")
    cons = DAQConsolidator(journal=j, producer_id="p", feed="f")
    tx = DAQTransmitter(face_url="http://face.local", source="src", schema_ref="s/v1",
                        token=None, transport=face, journal=j, batch_size=500)
    return Producer(reader=_Reader(n), consolidator=cons, transmitter=tx, **kw), j


def test_one_step_drains_a_backlog_far_larger_than_a_batch(tmp_path):
    face = _RealFace(tmp_path)
    # A budget no CI runner can exhaust: this is about draining, not speed.
    prod, j = _producer(tmp_path, face, 5000, send_budget_s=120)
    out = prod.step()
    assert out["journaled"] == 5000
    assert out["sent"] == 5000
    assert face.rows == 5000
    assert j.lag("transmitter") == 0


def test_a_face_that_says_wait_ends_the_step_after_one_try(tmp_path):
    class Busy:
        requests = 0

        def post(self, url, body, headers):
            Busy.requests += 1
            return 503, "busy"

    prod, j = _producer(tmp_path, Busy(), 2000)
    t = time.monotonic()
    out = prod.step()
    assert time.monotonic() - t < 2
    assert Busy.requests == 1 and out["sent"] == 0
    assert j.lag("transmitter") == 2000


def test_the_send_budget_bounds_a_step(tmp_path):
    face = _RealFace(tmp_path)
    prod, j = _producer(tmp_path, face, 5000, send_budget_s=0.0)
    out = prod.step()
    assert face.requests == 1  # always at least one request, then the budget stops it
    assert out["sent"] == 500
