# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""An ingest edge lands what producers push and holds it until a downstream pulls it.

The node that owns a data platform may sit on a network nobody outside can
reach, while the producers sending to it are only allowed to open connections
outward. The ingest edge is the public half: producers push to it, it writes
bronze, and the private node pulls from it. Every connection is opened by the
side that is allowed to open one.

What has to hold:

- every batch that landed new rows at the edge is in its outbox exactly once,
  in order, and a replayed batch adds nothing;
- a downstream that pulls the outbox lands the same rows, once, even when it is
  interrupted part way and when a record is delivered twice;
- only the downstream the edge is configured for can read the outbox: a
  producer's key cannot, even with a read grant, and an edge told nothing
  exports to nobody;
- a node composed as an ingest edge serves the ingest lanes and the export,
  and nothing else.
"""

from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
from pathlib import Path

import pytest

from axiom.rag.ingest_router import Disposition

from ...bronze import FilesystemTabularBronzeSink, TabularBronzeWriter
from ...ingest_sink import PushRowBatch, TabularIngestSink
from ...ingest_sink.edge import EdgeOutbox

ROWS = [{"channel": "TC1", "ts": f"2026-10-06T00:00:{i:02d}Z", "value": 20.0 + i, "unit": "degC"} for i in range(10)]


def _writer(root: Path) -> TabularBronzeWriter:
    return TabularBronzeWriter(
        rules=[],
        sink=FilesystemTabularBronzeSink(root=root),
        default_disposition=Disposition.ALLOW,
        default_tier="restricted",
    )


def _edge_sink(root: Path, outbox: EdgeOutbox) -> TabularIngestSink:
    return TabularIngestSink(writer=_writer(root), on_landed=outbox.record)


def _batch(item_id: str, rows=ROWS) -> PushRowBatch:
    return PushRowBatch(item_id=item_id, schema_ref="loop/rows-v1", rows=list(rows), metadata={"site": "site-a"})


def _landed_rows(root: Path, source: str) -> list[tuple[str, dict]]:
    """Each landed row with its row hash. Not the landing record's own time,
    which is when this node wrote it and is supposed to differ."""
    out: list[tuple[str, dict]] = []
    for f in sorted((root / source / "_rows").rglob("*.jsonl")):
        for line in f.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                out.append((rec["row_hash"], rec["row"]))
    return sorted(out, key=lambda t: t[0])


# -- the outbox ---------------------------------------------------------------


def test_a_landed_batch_is_recorded_once_and_a_replay_adds_nothing(tmp_path):
    outbox = EdgeOutbox(tmp_path / "edge")
    sink = _edge_sink(tmp_path / "bronze", outbox)

    sink.ingest_rows("site-a-src", [_batch("b1")])
    sink.ingest_rows("site-a-src", [_batch("b1")])  # the producer replays its spool

    records = outbox.read(after=0, limit=100)
    assert [r["seq"] for r in records] == [1]
    rec = records[0]
    assert rec["source"] == "site-a-src" and rec["item_id"] == "b1"
    assert rec["rows_landed"] == 10
    blob = outbox.content(rec["source"], rec["content_hash"], bronze_root=tmp_path / "bronze")
    assert hashlib.sha256(blob).hexdigest() == rec["content_hash"]


def test_the_sequence_survives_a_restart(tmp_path):
    EdgeOutbox(tmp_path / "edge").record(source="s", batch=_batch("b1"), content_hash="a" * 64, rows_landed=1)
    reopened = EdgeOutbox(tmp_path / "edge")
    reopened.record(source="s", batch=_batch("b2"), content_hash="b" * 64, rows_landed=1)
    assert [r["seq"] for r in reopened.read(after=0, limit=10)] == [1, 2]
    assert [r["seq"] for r in reopened.read(after=1, limit=10)] == [2]


# -- the pull ---------------------------------------------------------------


class _LocalEdge:
    """The edge as the puller sees it, without a socket: the same two reads."""

    def __init__(self, outbox: EdgeOutbox, bronze_root: Path, fail_after: int | None = None):
        self.outbox, self.bronze_root, self.fail_after, self.served = outbox, bronze_root, fail_after, 0

    def records(self, after: int, limit: int) -> list[dict]:
        return self.outbox.read(after=after, limit=limit)

    def content(self, source: str, content_hash: str) -> bytes:
        if self.fail_after is not None and self.served >= self.fail_after:
            raise ConnectionError("edge went away")
        self.served += 1
        return self.outbox.content(source, content_hash, bronze_root=self.bronze_root)


def test_a_downstream_pull_lands_every_row_once_across_an_interruption(tmp_path):
    from ...sources.edge import EdgePuller, FileCursor

    outbox = EdgeOutbox(tmp_path / "edge")
    edge_sink = _edge_sink(tmp_path / "edge-bronze", outbox)
    for i in range(5):
        edge_sink.ingest_rows("site-a-src", [_batch(f"b{i}", [dict(r, run=i) for r in ROWS])])

    down = TabularIngestSink(writer=_writer(tmp_path / "down-bronze"))
    cursor = FileCursor(tmp_path / "cursor.json")

    flaky = _LocalEdge(outbox, tmp_path / "edge-bronze", fail_after=2)
    with pytest.raises(ConnectionError):
        EdgePuller(flaky, sink_for=lambda s: down, cursor=cursor).pull()
    assert cursor.get() == 2  # what landed is remembered

    report = EdgePuller(_LocalEdge(outbox, tmp_path / "edge-bronze"), sink_for=lambda s: down, cursor=cursor).pull()
    assert report.records == 3 and cursor.get() == 5

    # Delivered twice (cursor lost): the rows still land once.
    FileCursor(tmp_path / "cursor.json").set(0)
    again = EdgePuller(_LocalEdge(outbox, tmp_path / "edge-bronze"), sink_for=lambda s: down, cursor=cursor).pull()
    assert again.rows_landed == 0 and again.rows_duplicate == 50

    assert len(_landed_rows(tmp_path / "down-bronze", "site-a-src")) == 50
    assert _landed_rows(tmp_path / "down-bronze", "site-a-src") == _landed_rows(tmp_path / "edge-bronze", "site-a-src")


def test_a_corrupted_blob_is_refused_not_landed(tmp_path):
    from ...sources.edge import EdgeIntegrityError, EdgePuller, FileCursor

    outbox = EdgeOutbox(tmp_path / "edge")
    _edge_sink(tmp_path / "edge-bronze", outbox).ingest_rows("src", [_batch("b1")])

    class _Tampered(_LocalEdge):
        def content(self, source, content_hash):
            return super().content(source, content_hash).replace(b"degC", b"degF")

    down = TabularIngestSink(writer=_writer(tmp_path / "down"))
    with pytest.raises(EdgeIntegrityError):
        EdgePuller(_Tampered(outbox, tmp_path / "edge-bronze"), sink_for=lambda s: down, cursor=FileCursor(tmp_path / "c.json")).pull()
    assert not (tmp_path / "down" / "src" / "_rows").exists()


# -- the node, end to end over a real socket ---------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def edge_node(tmp_path, monkeypatch):
    """A node composed as an ingest edge, with real issued keys, on a real port."""
    pytest.importorskip("uvicorn")
    import uvicorn

    from axiom.webauth import append_api_key_record, mint_api_key

    from ...agents.plinth.connectors import ConnectorConfig, save_connector

    state = tmp_path / "edge-state"
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    monkeypatch.setenv("AXIOM_INGEST_OUTBOX_DIR", str(tmp_path / "edge-outbox"))
    keys = tmp_path / "api-keys.json"
    monkeypatch.setenv("AXIOM_GATE_API_KEYS_FILE", str(keys))
    monkeypatch.setenv("AXIOM_MODE", "dev")
    monkeypatch.setenv("AXIOM_EDGE_DOWNSTREAM", "@downstream:org")
    for var in ("AXIOM_API_KEY", "AXIOM_HTTP_API_KEYS", "AXIOM_SERVE_INSECURE"):
        monkeypatch.delenv(var, raising=False)

    save_connector(
        ConnectorConfig(name="site-a-src", kind="push", bronze_root=str(tmp_path / "edge-bronze"),
                        site="site-a", default_disposition="allow", default_tier="restricted"),
        state_dir=state,
    )
    producer, rec = mint_api_key(principal="@daq:site-a", scopes=("data_platform:invoke", "*:read"), site="site-a")
    append_api_key_record(keys, rec)
    node, rec = mint_api_key(principal="@downstream:org", scopes=("edge_export:read",))
    append_api_key_record(keys, rec)

    from axiom.extensions.builtins.http.compose import compose_app
    from axiom.extensions.builtins.http.registry import RouterRegistry

    app = compose_app(profile="ingest-edge", registry=RouterRegistry(), bind_host="0.0.0.0")
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield {"url": f"http://127.0.0.1:{port}", "producer": producer, "node": node, "app": app,
           "edge_bronze": tmp_path / "edge-bronze"}
    server.should_exit = True
    t.join(timeout=5)


def _http(method, url, token, body=None):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - test loopback
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None


def test_the_edge_serves_ingest_and_export_and_nothing_else(edge_node):
    url, producer, node = edge_node["url"], edge_node["producer"], edge_node["node"]
    assert _http("GET", f"{url}/healthz", "")[0] == 200
    assert _http("GET", f"{url}/edge/outbox?after=0", node)[0] == 200
    # Everything a full node would serve is absent here, whoever asks.
    for path in ("/v1/models", "/classroom/x", "/herald/inbound/x", "/gate/keys", "/webapp/api/sites"):
        assert _http("GET", url + path, producer)[0] in (403, 404), path


def test_a_producer_pushes_and_the_downstream_pulls_the_same_rows(edge_node, tmp_path):
    from ...sources.edge import EdgePuller, FileCursor, HttpEdge

    url, producer, node = edge_node["url"], edge_node["producer"], edge_node["node"]
    for i in range(3):
        status, body = _http("POST", f"{url}/ingest/rows", producer, {
            "source": "site-a-src",
            "batches": [{"item_id": f"run-{i}", "schema_ref": "loop/rows-v1", "rows": [dict(r, run=i) for r in ROWS]}],
        })
        assert status == 200, body
        assert body["rows_landed"] == 10

    # A producer's key is bound to its site and may not read the export.
    assert _http("GET", f"{url}/edge/outbox?after=0", producer)[0] == 403
    # Health answers anyone, and says nothing but that the node is up.
    assert _http("GET", f"{url}/healthz", "")[0] == 200

    down = TabularIngestSink(writer=_writer(tmp_path / "down-bronze"))
    report = EdgePuller(HttpEdge(url, token=node), sink_for=lambda s: down,
                        cursor=FileCursor(tmp_path / "cursor.json")).pull()
    assert report.records == 3 and report.rows_landed == 30

    pulled = _landed_rows(tmp_path / "down-bronze", "site-a-src")
    assert pulled == _landed_rows(edge_node["edge_bronze"], "site-a-src")
    assert len(pulled) == 30


def test_an_edge_told_no_downstream_exports_to_nobody(edge_node, monkeypatch):
    monkeypatch.delenv("AXIOM_EDGE_DOWNSTREAM")
    assert _http("GET", f"{edge_node['url']}/edge/outbox?after=0", edge_node["node"])[0] == 403


def test_a_function_profile_leaves_off_a_mount_that_declares_no_function():
    pytest.importorskip("fastapi")
    from fastapi import APIRouter

    from axiom.extensions.builtins.http.registry import MountSpec, RouterRegistry

    reg = RouterRegistry()
    reg.register(MountSpec(prefix="/ingest", router=APIRouter(), extension="x", functions=("ingest",)))
    reg.register(MountSpec(prefix="/added-later", router=APIRouter(), extension="y"))
    assert [s.prefix for s in reg.specs(profile="ingest-edge")] == ["/ingest"]
    # Everywhere else, a mount with no declared function is served as before.
    assert [s.prefix for s in reg.specs(profile="server")] == ["/added-later", "/ingest"]


def test_the_downstream_pulls_through_its_edge_connector(edge_node, tmp_path, monkeypatch):
    """`axi data edge-pull <connector>`: the credential comes from a secret
    reference, each batch lands in the local connector of its source's name,
    and a second run finds nothing new."""
    import logging

    from axiom.infra.skills import SkillContext, SkillRegistry

    from ...agents.plinth.connectors import ConnectorConfig, save_connector
    from ...skills import edge_pull

    url, producer, node = edge_node["url"], edge_node["producer"], edge_node["node"]
    for i in range(2):
        assert _http("POST", f"{url}/ingest/rows", producer, {
            "source": "site-a-src",
            "batches": [{"item_id": f"run-{i}", "schema_ref": "loop/rows-v1", "rows": [dict(r, run=i) for r in ROWS]}],
        })[0] == 200

    down_state = tmp_path / "down-state"
    monkeypatch.setenv("EDGE_PULL_TEST_TOKEN", node)
    save_connector(ConnectorConfig(name="the-edge", kind="edge", bronze_root=str(tmp_path / "edge-meta"),
                                   credential_ref="env://EDGE_PULL_TEST_TOKEN", params={"edge_url": url}),
                   state_dir=down_state)
    save_connector(ConnectorConfig(name="site-a-src", kind="push", bronze_root=str(tmp_path / "down-bronze"),
                                   site="site-a", default_disposition="allow", default_tier="restricted"),
                   state_dir=down_state)
    monkeypatch.delenv("AXIOM_INGEST_OUTBOX_DIR")  # the downstream is not an edge

    ctx = SkillContext(registry=SkillRegistry(), state_dir=down_state, logger=logging.getLogger("t"))
    first = edge_pull.run({"connector": "the-edge", "state_dir": str(down_state)}, ctx)
    assert first.ok, first.errors
    assert first.value["records"] == 2 and first.value["rows_landed"] == 20
    assert node not in json.dumps(first.value) + " ".join(first.actions_taken)

    second = edge_pull.run({"connector": "the-edge", "state_dir": str(down_state)}, ctx)
    assert second.ok and second.value["records"] == 0
    assert _landed_rows(tmp_path / "down-bronze", "site-a-src") == _landed_rows(edge_node["edge_bronze"], "site-a-src")


def test_an_edge_behind_a_proxy_takes_no_anonymous_write(tmp_path, monkeypatch):
    """The edge binds loopback and a TLS proxy forwards the internet to it, so
    every request arrives from 127.0.0.1. The loopback convenience that lets a
    developer's local node answer without a credential must not apply here:
    found 2026-10-07, an anonymous push to a loopback-bound edge landed."""
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

    client = TestClient(compose_app(profile="ingest-edge", registry=RouterRegistry(), bind_host="127.0.0.1"))
    body = {"source": "src", "batches": [{"item_id": "x", "schema_ref": "s", "rows": [{"a": 1}]}]}
    assert client.post("/ingest/rows", json=body).status_code in (401, 403)
    assert client.post("/ingest/rows", json=body, headers={"Authorization": f"Bearer {token}"}).status_code == 200
