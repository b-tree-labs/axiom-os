# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``POST /ingest/rows`` — the row lane's HTTP front door (ADR-106 P0).

The document lane's peer, exercised the same way as ``test_ingest_sink.py``:
in-process via ``TestClient``, real ``TabularBronzeWriter`` over a tmp bronze
root, no Postgres, no bound port.
"""

from __future__ import annotations

import pytest

from axiom.rag.ingest_router import Disposition, ProvenanceRule

from ...bronze import FilesystemTabularBronzeSink, TabularBronzeWriter
from ...ingest_sink import TabularIngestSink


def _writer(tmp_path, rules=None, default=Disposition.ALLOW):
    return TabularBronzeWriter(
        rules=rules or [],
        sink=FilesystemTabularBronzeSink(root=tmp_path / "bronze"),
        default_disposition=default,
        default_tier="rag-org",
    )


def _client(resolver=None, sink=None):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.http.server import create_app

    from ...ingest_sink.api import build_tabular_ingest_router

    app = create_app(title="t", version="0", description="")
    app.include_router(build_tabular_ingest_router(sink=sink, sink_resolver=resolver))
    return TestClient(app)


def _batch(item_id="batch-1", rows=None, **kw):
    body = {
        "item_id": item_id,
        "schema_ref": "test/rows-v1",
        "rows": rows if rows is not None else [{"a": 1}, {"a": 2}],
    }
    body.update(kw)
    return body


# --- happy path -----------------------------------------------------------


def test_push_rows_lands_in_bronze(tmp_path):
    """A pushed batch lands through the same writer the pull path uses."""
    client = _client(sink=TabularIngestSink(writer=_writer(tmp_path)))

    resp = client.post("/ingest/rows", json={"source": "unit-src", "batches": [_batch()]})

    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] == 1
    assert body["landed"] == 1
    assert body["rows_in"] == 2
    assert body["rows_landed"] == 2
    assert body["rows_duplicate"] == 0
    assert list((tmp_path / "bronze" / "unit-src" / "_rows").rglob("*.jsonl"))


def test_push_carries_optional_fields(tmp_path):
    """etag / source_path / metadata ride through to the RowBatch."""
    client = _client(sink=TabularIngestSink(writer=_writer(tmp_path)))

    resp = client.post(
        "/ingest/rows",
        json={
            "source": "unit-src",
            "batches": [
                _batch(
                    etag='W/"v1"',
                    source_path="feed/current",
                    metadata={"producer": "bridge-1"},
                )
            ],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["rows_landed"] == 2


def test_duplicate_push_is_idempotent(tmp_path):
    """At-least-once delivery is safe: replaying a batch lands it once (ADR-106 §4)."""
    sink = TabularIngestSink(writer=_writer(tmp_path))
    client = _client(sink=sink)
    payload = {"source": "unit-src", "batches": [_batch()]}

    first = client.post("/ingest/rows", json=payload)
    second = client.post("/ingest/rows", json=payload)

    assert first.json()["rows_landed"] == 2
    assert second.json()["rows_landed"] == 0
    assert second.json()["rows_duplicate"] == 2


def test_multiple_batches_in_one_request(tmp_path):
    client = _client(sink=TabularIngestSink(writer=_writer(tmp_path)))

    resp = client.post(
        "/ingest/rows",
        json={
            "source": "unit-src",
            "batches": [_batch("b1"), _batch("b2", rows=[{"a": 9}])],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 2
    assert resp.json()["rows_in"] == 3


# --- provenance gate ------------------------------------------------------


def test_excluded_batch_is_gated_not_landed(tmp_path):
    """The provenance gate applies to pushed rows exactly as to pulled ones."""
    rules = [
        ProvenanceRule(
            pattern="*/secret/*", disposition=Disposition.EXCLUDE, reason="test exclusion"
        )
    ]
    client = _client(sink=TabularIngestSink(writer=_writer(tmp_path, rules=rules)))

    resp = client.post(
        "/ingest/rows",
        json={"source": "unit-src", "batches": [_batch(source_path="/feeds/secret/rows.json")]},
    )
    assert resp.status_code == 200
    assert resp.json()["excluded"] == 1
    assert resp.json()["rows_landed"] == 0


# --- resolver / routing ---------------------------------------------------


def test_resolver_routes_per_source(tmp_path):
    """Each source resolves to its own connector sink — no split brain."""
    root_a = tmp_path / "a"
    sink_a = TabularIngestSink(
        writer=TabularBronzeWriter(
            rules=[],
            sink=FilesystemTabularBronzeSink(root=root_a / "bronze"),
            default_disposition=Disposition.ALLOW,
            default_tier="rag-org",
        )
    )

    def resolver(source: str):
        if source == "conn-a":
            return sink_a
        raise KeyError(source)

    client = _client(resolver=resolver)
    ok = client.post("/ingest/rows", json={"source": "conn-a", "batches": [_batch()]})

    assert ok.status_code == 200
    assert ok.json()["landed"] == 1
    assert list((root_a / "bronze").rglob("*.jsonl"))


def test_unknown_source_is_loud_422():
    """An unknown connector fails loudly rather than quarantining into a rule-less tree."""

    def resolver(source: str):
        raise KeyError(source)

    client = _client(resolver=resolver)
    resp = client.post("/ingest/rows", json={"source": "nope", "batches": [_batch()]})

    assert resp.status_code == 422
    assert "unknown connector" in resp.json()["detail"]


def test_missing_source_is_422(tmp_path):
    client = _client(sink=TabularIngestSink(writer=_writer(tmp_path)))
    resp = client.post("/ingest/rows", json={"source": "", "batches": [_batch()]})
    assert resp.status_code == 422


def test_router_requires_sink_or_resolver():
    pytest.importorskip("fastapi")
    from ...ingest_sink.api import build_tabular_ingest_router

    with pytest.raises(ValueError):
        build_tabular_ingest_router()


# --- request bounds (DoS) -------------------------------------------------


def test_too_many_batches_rejected_at_validation(tmp_path, monkeypatch):
    """Request-shape caps reject oversized pushes before any write (ADR-106 §6)."""
    pytest.importorskip("fastapi")
    from ...ingest_sink import api as api_mod

    monkeypatch.setattr(api_mod, "_MAX_BATCHES", 2)
    client = _client(sink=TabularIngestSink(writer=_writer(tmp_path)))

    resp = client.post(
        "/ingest/rows",
        json={"source": "unit-src", "batches": [_batch(f"b{i}") for i in range(3)]},
    )
    assert resp.status_code == 422


def test_too_many_rows_in_batch_rejected(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from ...ingest_sink import api as api_mod

    monkeypatch.setattr(api_mod, "_MAX_ROWS_PER_BATCH", 2)
    client = _client(sink=TabularIngestSink(writer=_writer(tmp_path)))

    resp = client.post(
        "/ingest/rows",
        json={"source": "unit-src", "batches": [_batch(rows=[{"a": i} for i in range(3)])]},
    )
    assert resp.status_code == 422


def test_schema_ref_is_required(tmp_path):
    """A batch without a declared schema is rejected — bronze rows are typed."""
    client = _client(sink=TabularIngestSink(writer=_writer(tmp_path)))
    resp = client.post(
        "/ingest/rows",
        json={"source": "unit-src", "batches": [{"item_id": "b1", "rows": [{"a": 1}]}]},
    )
    assert resp.status_code == 422


# --- both lanes on one face (ADR-106 §1) ----------------------------------


def test_document_and_row_lanes_mount_together(tmp_path):
    """/ingest and /ingest/rows are peers on one app — one face, two lanes."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.http.server import create_app

    from ...bronze import BronzeWriter, FilesystemBronzeSink
    from ...ingest_sink import IngestSink
    from ...ingest_sink.api import build_ingest_router, build_tabular_ingest_router

    docs = IngestSink(
        writer=BronzeWriter(
            rules=[],
            sink=FilesystemBronzeSink(root=tmp_path / "docs"),
            default_disposition=Disposition.ALLOW,
            default_tier="rag-org",
        )
    )
    rows = TabularIngestSink(writer=_writer(tmp_path))

    app = create_app(title="t", version="0", description="")
    app.include_router(build_ingest_router(sink=docs))
    app.include_router(build_tabular_ingest_router(sink=rows))
    client = TestClient(app)

    assert (
        client.post(
            "/ingest", json={"source": "s", "items": [{"item_id": "d1", "content": "hi"}]}
        ).status_code
        == 200
    )
    assert (
        client.post("/ingest/rows", json={"source": "s", "batches": [_batch()]}).status_code == 200
    )
