# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A site's backlog cannot fill the node's disk or starve its own live data.

A collector that was offline for weeks reconnects with a backlog and sends it
as fast as the face allows. Two things must hold while it does.

**Live data still gets in.** A request the forwarder marks as backlog
(``X-Axiom-Lane: backlog``) spends its own, smaller per-site budget, so a
drain that runs its budget dry is refused with 429 while the same site's live
pushes keep landing.

**The disk is protected before it is full.** Below a free-space floor the face
refuses every data push with 507 and a ``Retry-After``, before anything is
written, and says why. The floor is measured on the real filesystem the node
writes to. A heartbeat is not data and still gets in, so the platform keeps
hearing from a node it is telling to wait.
"""

from __future__ import annotations

import shutil

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from axiom.infra.ratelimit import BucketRegistry  # noqa: E402

from ..api import build_tabular_ingest_router  # noqa: E402
from ..tabular import TabularIngestResult  # noqa: E402


class _Sink:
    def __init__(self):
        self.calls = 0

    def ingest_rows(self, source, batches):
        self.calls += 1
        rows = sum(len(b.rows) for b in batches)
        return TabularIngestResult(source=source, accepted=len(batches), landed=len(batches),
                                   excluded=0, errored=0, rows_in=rows, rows_landed=rows,
                                   rows_duplicate=0)


class _Policy:
    def grant_for(self, request):
        from ..tenancy import IngestGrant

        return IngestGrant(principal="@p:site-a", site="site-a", max_access_tier=None)

    def check(self, grant, metadata):
        return {**metadata, "site": grant.site}


def _client(sink, *, backlog=None, live=None):
    app = FastAPI()
    app.include_router(build_tabular_ingest_router(
        sink, tenancy=_Policy(),
        limits=live or BucketRegistry(capacity=1000, refill_per_second=1000),
        backlog_limits=backlog))
    return TestClient(app)


def _body(item="i1"):
    return {"source": "src", "batches": [{"item_id": item, "schema_ref": "s/v1",
                                          "rows": [{"channel": "c", "ts": "2026-10-08T00:00:00Z", "value": 1}]}]}


def test_a_drained_backlog_budget_leaves_live_pushes_landing(monkeypatch, tmp_path):
    monkeypatch.setenv("AXIOM_INGEST_HEADROOM_PATH", str(tmp_path))
    sink = _Sink()
    client = _client(sink, backlog=BucketRegistry(capacity=3, refill_per_second=0.001))
    backlog = [client.post("/ingest/rows", json=_body(f"b{i}"), headers={"X-Axiom-Lane": "backlog"})
               for i in range(5)]
    assert [r.status_code for r in backlog] == [200, 200, 200, 429, 429]
    assert "Retry-After" in backlog[-1].headers
    live = [client.post("/ingest/rows", json=_body(f"l{i}")) for i in range(5)]
    assert [r.status_code for r in live] == [200] * 5


def test_below_the_free_space_floor_data_is_refused_before_any_write(monkeypatch, tmp_path):
    free = shutil.disk_usage(tmp_path).free
    monkeypatch.setenv("AXIOM_INGEST_HEADROOM_PATH", str(tmp_path))
    monkeypatch.setenv("AXIOM_INGEST_MIN_FREE_BYTES", str(free + 10**9))  # the real disk is "below"
    sink = _Sink()
    r = _client(sink).post("/ingest/rows", json=_body())
    assert r.status_code == 507
    assert int(r.headers["Retry-After"]) >= 1
    assert "free" in r.json()["detail"]
    assert sink.calls == 0, "nothing may be written below the floor"


def test_a_heartbeat_still_gets_in_when_the_disk_is_low(monkeypatch, tmp_path):
    free = shutil.disk_usage(tmp_path).free
    monkeypatch.setenv("AXIOM_INGEST_HEADROOM_PATH", str(tmp_path))
    monkeypatch.setenv("AXIOM_INGEST_MIN_FREE_BYTES", str(free + 10**9))
    monkeypatch.setenv("AXIOM_HEARTBEAT_DIR", str(tmp_path / "beats"))
    beat = {"source": "src", "batches": [{"item_id": "hb", "schema_ref": "axiom.node-heartbeat/v1",
                                          "rows": [{"node": "n1", "lanes": "ok"}]}]}
    assert _client(_Sink()).post("/ingest/rows", json=beat).status_code == 200


def test_with_the_floor_unset_a_healthy_disk_takes_data(monkeypatch, tmp_path):
    monkeypatch.setenv("AXIOM_INGEST_HEADROOM_PATH", str(tmp_path))
    monkeypatch.delenv("AXIOM_INGEST_MIN_FREE_BYTES", raising=False)
    monkeypatch.delenv("AXIOM_INGEST_MIN_FREE_PERCENT", raising=False)
    assert _client(_Sink()).post("/ingest/rows", json=_body()).status_code == 200


def test_the_disk_alarm_sounds_while_data_is_still_accepted(monkeypatch, tmp_path):
    """The alarm floor sits above the refusal floor: a person hears before anything is refused."""
    from ..headroom import headroom

    free = shutil.disk_usage(tmp_path).free
    monkeypatch.setenv("AXIOM_INGEST_MIN_FREE_BYTES", "1")
    monkeypatch.setenv("AXIOM_INGEST_MIN_FREE_PERCENT", "0")
    monkeypatch.setenv("AXIOM_DISK_ALARM_FREE_BYTES", str(free + 10**9))
    room = headroom(tmp_path)
    assert room["ok"] is True and room["alarm"] is True
    monkeypatch.setenv("AXIOM_DISK_ALARM_FREE_BYTES", "1")
    monkeypatch.setenv("AXIOM_DISK_ALARM_FREE_PERCENT", "0")
    assert headroom(tmp_path)["alarm"] is False
    # A directory not created yet is measured on the disk it will live on.
    assert headroom(tmp_path / "not" / "yet")["free_bytes"] > 0
